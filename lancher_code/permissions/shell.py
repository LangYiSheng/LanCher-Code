"""只解析命令，不执行模型提供的代码；不确定的语义必须交给审批。"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal


MAX_COMMAND_BYTES = 128 * 1024
MAX_AST_NODES = 20_000
Operation = Literal["read", "write", "delete", "cwd", "execute"]


@dataclass(frozen=True, slots=True)
class AnalyzedCommand:
    raw: str
    argv: tuple[str, ...]
    name: str
    cwd: Path
    dynamic: bool = False


@dataclass(frozen=True, slots=True)
class PathAccess:
    raw: str
    path: Path
    operation: Operation
    command_index: int
    recursive: bool = False


@dataclass(frozen=True, slots=True)
class DeleteTarget:
    raw: str
    path: Path
    recursive: bool
    command_index: int


@dataclass(frozen=True, slots=True)
class CommandAnalysis:
    raw_command: str
    shell: str
    commands: tuple[AnalyzedCommand, ...] = ()
    path_accesses: tuple[PathAccess, ...] = ()
    unknown_reasons: tuple[str, ...] = ()
    hard_denials: tuple[str, ...] = ()
    deletes: tuple[DeleteTarget, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.unknown_reasons


@dataclass(frozen=True, slots=True)
class _SyntaxCommand:
    raw: str
    argv: tuple[str, ...]
    offset: int
    dynamic: bool = False
    path_parameters: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _SyntaxRedirect:
    raw: str | None
    offset: int
    write: bool


@dataclass(frozen=True, slots=True)
class _Syntax:
    commands: tuple[_SyntaxCommand, ...] = ()
    redirects: tuple[_SyntaxRedirect, ...] = ()
    unknown: tuple[str, ...] = ()


# 原始命令只从 stdin 读取并传给 Parser，永远不插入这个固定脚本。
_POWERSHELL_PARSER = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$source = [Console]::In.ReadToEnd()
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors)
function Static-Values($node) {
    if ($node -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
        $node -is [System.Management.Automation.Language.ConstantExpressionAst]) { return ,([string]$node.Value) }
    if ($node -is [System.Management.Automation.Language.ExpandableStringExpressionAst] -and
        $node.NestedExpressions.Count -eq 0) { return ,([string]$node.Value) }
    if ($node -is [System.Management.Automation.Language.ArrayLiteralAst]) {
        $result = @()
        foreach ($element in $node.Elements) {
            $value = Static-Values $element
            if ($null -eq $value) { return $null }
            $result += $value
        }
        return ,$result
    }
    return $null
}
$commands = @($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.CommandAst]}, $true) | ForEach-Object {
    $args = @(); $dynamic = $false; $paths = @(); $pathParameter = $false
    foreach ($element in $_.CommandElements) {
        if ($element -is [System.Management.Automation.Language.CommandParameterAst]) {
            $args += ('-' + $element.ParameterName)
            $pathParameter = $element.ParameterName -in @('Path','LiteralPath','FilePath')
            if ($null -ne $element.Argument) {
                $value = Static-Values $element.Argument
                if ($null -eq $value) { $dynamic = $true } else { $args += $value }
                if ($pathParameter -and $null -ne $value) { $paths += $value }
                $pathParameter = $false
            }
        } else {
            $value = Static-Values $element
            if ($null -eq $value) { $dynamic = $true } else { $args += $value }
            if ($pathParameter -and $null -ne $value) { $paths += $value }
            $pathParameter = $false
        }
    }
    @{raw=$_.Extent.Text; offset=$_.Extent.StartOffset; argv=@($args); dynamic=$dynamic; paths=@($paths)}
})
$redirects = @($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FileRedirectionAst]}, $true) | ForEach-Object {
        $value = @(Static-Values $_.Location)
    @{raw=$(if ($value.Count -ne 1 -or $null -eq $value[0]) {$null} else {$value[0]}); offset=$_.Extent.StartOffset; write=$true}
})
$uncertain = @($ast.FindAll({param($n)
    $n.GetType().Name -in @('IfStatementAst','ForStatementAst','ForEachStatementAst','WhileStatementAst',
      'DoWhileStatementAst','DoUntilStatementAst','SwitchStatementAst','TryStatementAst','FunctionDefinitionAst',
      'ScriptBlockExpressionAst','AssignmentStatementAst','SubExpressionAst','UsingExpressionAst','PipelineChainAst',
      'CommandExpressionAst','InvokeMemberExpressionAst','MemberExpressionAst','TypeDefinitionAst',
      'TrapStatementAst','DataStatementAst','ConfigurationDefinitionAst')
}, $true))
$nodes = @($ast.FindAll({param($n) $true}, $true)).Count
@{commands=@($commands); redirects=@($redirects); errors=@($errors | ForEach-Object {$_.Message});
  uncertain=($uncertain.Count -gt 0); nodes=$nodes} | ConvertTo-Json -Depth 10 -Compress
"""


def default_shell() -> str:
    return "powershell" if os.name == "nt" else "sh"


def _powershell_executable() -> str | None:
    # 与 Windows 执行后端采用同一个解释器版本，避免把 PS 7 语法当成 PS 5 语法。
    if os.name == "nt":
        candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        return str(candidate) if candidate.is_file() else None
    return shutil.which("pwsh")


def _parse_powershell(command: str) -> _Syntax:
    executable = _powershell_executable()
    if executable is None:
        return _Syntax(unknown=("PowerShell AST 解析器不可用。",))
    encoded = base64.b64encode(_POWERSHELL_PARSER.encode("utf-16-le")).decode("ascii")
    try:
        process = subprocess.run(
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            input=command.encode("utf-8"), capture_output=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        if process.returncode != 0:
            return _Syntax(unknown=("PowerShell AST 解析失败。",))
        document = json.loads(process.stdout.decode("utf-8-sig"))
        if document["nodes"] > MAX_AST_NODES:
            return _Syntax(unknown=("命令 AST 超出完整分析预算。",))
        unknown = []
        if document["errors"]:
            unknown.append("PowerShell 命令存在语法错误。")
        if document["uncertain"]:
            unknown.append("命令包含分支、循环、脚本块、赋值、方法或未支持表达式，无法静态确定执行上下文。")
        commands = tuple(_SyntaxCommand(item["raw"], tuple(item["argv"]), item["offset"], item["dynamic"], tuple(item["paths"]))
                         for item in document["commands"])
        redirects = tuple(_SyntaxRedirect(item["raw"], item["offset"], item["write"])
                          for item in document["redirects"])
        return _Syntax(commands, redirects, tuple(unknown))
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return _Syntax(unknown=("PowerShell AST 解析失败或超时。",))


def _parse_bash(command: str) -> _Syntax:
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_bash
    except ImportError:
        return _Syntax(unknown=("Bash AST 解析器不可用。",))
    source = command.encode("utf-8")
    try:
        tree = Parser(Language(tree_sitter_bash.language())).parse(source)
        unknown = ["Shell 命令存在语法错误。"] if tree.root_node.has_error else []
        commands: list[_SyntaxCommand] = []
        redirects: list[_SyntaxRedirect] = []
        stack = [tree.root_node]
        count = 0
        while stack:
            node = stack.pop()
            count += 1
            if count > MAX_AST_NODES:
                return _Syntax(unknown=("命令 AST 超出完整分析预算。",))
            stack.extend(reversed(node.named_children))
            if node.type in {"if_statement", "for_statement", "while_statement", "case_statement", "function_definition",
                             "subshell", "command_substitution", "process_substitution", "variable_assignment", "heredoc_redirect"}:
                unknown.append("命令包含分支、循环、变量、子进程或脚本，无法静态确定执行上下文。")
            if any(child.type in {"||", "&"} for child in node.children):
                unknown.append("条件失败分支或后台执行会改变后续命令的执行上下文。")
            if node.type == "pipeline" and any(
                source[part.start_byte:part.end_byte].decode("utf-8").strip().startswith("cd ")
                for part in node.named_children
            ):
                unknown.append("管道中的目录变化在子进程内生效，无法作为后续命令的工作目录。")
            if node.type == "command":
                argv = []
                dynamic = False
                for child in node.named_children:
                    if child.type in {"file_redirect", "heredoc_redirect"}:
                        continue
                    text = source[child.start_byte:child.end_byte].decode("utf-8")
                    if child.type == "concatenation" and "{" in text and "}" in text:
                        dynamic = True
                    nested = [child]
                    while nested:
                        part = nested.pop()
                        if part.type in {"expansion", "simple_expansion", "command_substitution", "process_substitution", "variable_assignment", "brace_expression"}:
                            dynamic = True
                        nested.extend(part.named_children)
                    try:
                        words = shlex.split(text, posix=True)
                    except ValueError:
                        words = []
                        dynamic = True
                    if len(words) != 1:
                        dynamic = True
                    argv.extend(words)
                raw_node = node.parent if node.parent is not None and node.parent.type == "redirected_statement" else node
                commands.append(_SyntaxCommand(source[raw_node.start_byte:raw_node.end_byte].decode("utf-8"), tuple(argv), node.start_byte, dynamic))
            if node.type == "file_redirect":
                target = node.child_by_field_name("destination")
                text = source[node.start_byte:node.end_byte].decode("utf-8")
                value: str | None = None
                if target is not None:
                    raw = source[target.start_byte:target.end_byte].decode("utf-8")
                    try:
                        values = shlex.split(raw)
                        if len(values) == 1 and "$" not in raw and "`" not in raw:
                            value = values[0]
                    except ValueError:
                        pass
                # 2>&1 只重定向描述符；不把数字当成路径。
                if ">&" in text or "<&" in text:
                    continue
                redirects.append(_SyntaxRedirect(value, node.start_byte, ">" in text))
        return _Syntax(tuple(commands), tuple(redirects), tuple(dict.fromkeys(unknown)))
    except (ValueError, TypeError, AttributeError, RuntimeError):
        return _Syntax(unknown=("Bash AST 解析失败。",))


@lru_cache(maxsize=128)
def _parse(command: str, shell: str) -> _Syntax:
    # 只缓存纯语法；目标路径和链接必须在每次调用中重新解析。
    return _parse_powershell(command) if shell == "powershell" else _parse_bash(command)


_PS_ALIASES = {
    "rm": "remove-item", "del": "remove-item", "erase": "remove-item", "rd": "remove-item", "rmdir": "remove-item", "ri": "remove-item",
    "cd": "set-location", "chdir": "set-location", "sl": "set-location",
    "cat": "get-content", "gc": "get-content", "type": "get-content",
    "ls": "get-childitem", "dir": "get-childitem", "gci": "get-childitem", "gi": "get-item",
    "sc": "set-content", "ac": "add-content", "ni": "new-item",
    "cp": "copy-item", "copy": "copy-item", "cpi": "copy-item", "mv": "move-item", "move": "move-item", "mi": "move-item",
    "echo": "write-output", "pwd": "get-location", "iex": "invoke-expression", "saps": "start-process", "start": "start-process",
}
_HARMLESS = {"write-output", "write-host", "get-location", "echo", "printf", "pwd", "true", "false", "test", "[", "get-date", "start-sleep", "sleep"}
_READ = {"get-content", "get-item", "get-childitem", "cat", "ls", "head", "tail", "stat", "wc", "du", "file", "test-path"}
_WRITE = {"set-content", "add-content", "out-file", "new-item", "touch", "mkdir"}
_DELETE = {"remove-item", "rm", "rmdir", "unlink"}
_INTERPRETERS = {"python", "python3", "python3.14", "py", "node", "ruby", "perl", "php", "bash", "sh", "zsh", "fish", "powershell", "pwsh", "cmd", "invoke-expression", "start-process", "invoke-command", "eval", "exec", "source", ".", "xargs", "find"}


def _name(value: str, shell: str) -> str:
    # 明确指向路径的程序仍标记为未知，basename 只用于发现高风险操作。
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    if shell == "powershell":
        name = name.casefold()
        for suffix in (".exe", ".com", ".cmd", ".bat"):
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                break
        name = _PS_ALIASES.get(name, name)
    return name


def canonical_command_name(value: str, shell: str) -> str:
    """供权限规则规范化命令名称；不修改任何参数文本。"""
    return _name(value, shell)


def _looks_like_path(value: str) -> bool:
    return value.startswith((".", "/", "\\", "~")) or "/" in value or "\\" in value or bool(re.match(r"^[A-Za-z]:", value))


def _arguments(argv: tuple[str, ...], shell: str, name: str) -> tuple[list[str], dict[str, str], bool]:
    """保留参数值，只把 PowerShell 参数名称规范化。"""
    positional: list[str] = []
    options: dict[str, str] = {}
    uncertain = False
    value_parameters = {"path", "literalpath", "destination", "filepath", "value", "filter", "include", "exclude", "encoding", "itemtype", "name", "erroraction"}
    switches = {"recurse", "force", "whatif", "confirm", "verbose", "debug", "passthru", "append", "noclobber", "nonewline", "raw", "directory", "file", "hidden"}
    index = 1
    literal = False
    while index < len(argv):
        value = argv[index]
        if value == "--":
            literal = True
        elif not literal and value.startswith("-"):
            if shell == "powershell":
                key = value.lstrip("-").casefold()
                if key in value_parameters and index + 1 < len(argv):
                    index += 1
                    options[key] = argv[index]
                elif key in switches:
                    options[key] = "true"
                else:
                    uncertain = True
            else:
                if name in {"cp", "mv"} and value in {"-t", "--target-directory"}:
                    if index + 1 < len(argv):
                        index += 1
                        options["destination"] = argv[index]
                    else:
                        uncertain = True
                elif name in {"cp", "mv"} and value.startswith("--target-directory="):
                    options["destination"] = value.split("=", 1)[1]
                else:
                    options[value] = "true"
        else:
            positional.append(value)
        index += 1
    return positional, options, uncertain


def _hard_denial(name: str, argv: tuple[str, ...]) -> str | None:
    if name in {"format", "format-volume", "clear-disk", "initialize-disk", "remove-partition", "diskpart", "shutdown", "restart-computer", "stop-computer", "sudo", "runas", "mkfs", "mkswap", "fdisk", "parted"} or name.startswith("mkfs."):
        return f"系统高风险操作 {name} 禁止执行。"
    if name == "cipher" and any(value.casefold().startswith("/w") for value in argv[1:]):
        return "禁止执行磁盘数据擦除操作。"
    if name == "git":
        args = [value.casefold() for value in argv[1:]]
        # Git 全局参数可放在子命令前；不能仅检查 argv[1]。
        while args and args[0].startswith("-"):
            option = args.pop(0)
            if option in {"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"} and args:
                args.pop(0)
        if args and ((args[0] == "reset" and "--hard" in args) or
                     (args[0] == "clean" and any(value == "--force" or (value.startswith("-") and not value.startswith("--") and "f" in value) for value in args[1:])) or
                     (args[0] == "checkout" and "--" in args) or
                     (args[0] == "restore" and any(value == "--source" or value.startswith("--source=") for value in args[1:]))):
            return "禁止执行丢弃工作区内容的 Git 操作。"
    return None


def _git_branch_is_listing(argv: tuple[str, ...]) -> bool:
    """branch 同时有查询、修改和编辑器入口，只认可明确的查询参数。"""
    arguments = list(argv[2:])
    list_patterns = any(value == "--list" or value.startswith("--list=") for value in arguments)
    switches = {"-a", "-r", "-v", "-vv", "--all", "--remotes", "--verbose", "--list", "--show-current", "--no-color", "--column", "--no-column"}
    value_options = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--format", "--sort", "--abbrev", "--color"}
    index = 0
    while index < len(arguments):
        value = arguments[index]
        option = value.split("=", 1)[0]
        if value in switches or ("=" in value and option in value_options | {"--list", "--column"}):
            index += 1
            continue
        if option in value_options:
            index += 2
            continue
        if value.startswith("-") or not list_patterns:
            return False
        index += 1
    return True


def analyze_command(command: str, *, shell: str | None = None, cwd: Path, project_root: Path) -> CommandAnalysis:
    """对完整原文解析，对可确定路径实时 resolve；范围限制由权限引擎决定。"""
    selected = shell or default_shell()
    if selected in {"pwsh", "powershell.exe"}:
        selected = "powershell"
    if selected not in {"powershell", "bash", "sh", "posix"}:
        return CommandAnalysis(command, selected, unknown_reasons=("Shell 类型无法解析。",))
    if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
        return CommandAnalysis(command, selected, unknown_reasons=("命令长度超出完整分析预算。",))
    syntax = _parse(command, "powershell" if selected == "powershell" else "bash")
    unknown = list(syntax.unknown)
    commands: list[AnalyzedCommand] = []
    paths: list[PathAccess] = []
    deletes: list[DeleteTarget] = []
    denials: list[str] = []
    current_cwd = cwd.resolve()

    def add_path(raw: str, operation: Operation, index: int, recursive: bool = False) -> Path | None:
        if not raw or "\x00" in raw:
            unknown.append("路径为空或包含无效字符。")
            return None
        if raw.startswith("~") or any(char in raw for char in "*?[") or (selected == "powershell" and ":" in raw and not re.match(r"^[A-Za-z]:[\\/]", raw)):
            unknown.append(f"路径需要运行时展开或涉及非文件系统提供程序：{raw}")
        try:
            target = Path(raw)
            if not target.is_absolute():
                target = current_cwd / target
            target = target.resolve()
        except (OSError, RuntimeError, ValueError):
            unknown.append(f"无法解析路径：{raw}")
            return None
        paths.append(PathAccess(raw, target, operation, index, recursive))
        if operation == "delete":
            deletes.append(DeleteTarget(raw, target, recursive, index))
        return target

    ordered = sorted(syntax.commands, key=lambda item: item.offset)
    command_cwds: list[Path] = []
    for index, item in enumerate(ordered):
        command_cwds.append(current_cwd)
        name = _name(item.argv[0], selected) if item.argv else ""
        commands.append(AnalyzedCommand(item.raw, item.argv, name, current_cwd, item.dynamic))
        if item.dynamic or not item.argv:
            unknown.append("子命令包含动态参数或动态命令名称。")
        denial = _hard_denial(name, item.argv)
        if denial:
            denials.append(denial)
        positional, options, uncertain_options = _arguments(item.argv, selected, name)
        is_path_command = name in _READ | _WRITE | _DELETE | {"cd", "set-location", "copy-item", "move-item", "cp", "mv"}
        if is_path_command and uncertain_options:
            unknown.append(f"{name} 包含不能静态解释的参数。")
        targets = list(item.path_parameters) or [options[key] for key in ("literalpath", "path", "filepath") if key in options]
        if name in {"cd", "set-location"}:
            unknown.append("目录切换可能失败，后续相对路径需要核对实际工作目录。")
            targets = targets or positional[:1]
            if len(targets) != 1:
                unknown.append("目录变化无法静态确定。")
            else:
                next_cwd = add_path(targets[0], "cwd", index)
                if next_cwd is not None:
                    current_cwd = next_cwd
        elif name in _DELETE:
            targets = list(dict.fromkeys(targets + positional))
            recursive = "recurse" in options if selected == "powershell" else any(
                value == "--recursive" or (value.startswith("-") and not value.startswith("--") and "r" in value.lower())
                for value in item.argv[1:]
            )
            if any(key in options for key in ("filter", "include", "exclude")):
                unknown.append("删除范围受运行时筛选条件影响。")
            if not targets:
                unknown.append("删除目标由管道或运行时提供，无法完整列出。")
            for target in targets:
                add_path(target, "delete", index, recursive)
                if name == "rmdir" and selected != "powershell" and (
                    "--parents" in options or any(value.startswith("-") and not value.startswith("--") and "p" in value for value in item.argv[1:])
                ):
                    for parent in Path(target).parents:
                        if str(parent) == ".":
                            break
                        add_path(str(parent), "delete", index)
        elif name in _READ:
            for target in dict.fromkeys(targets + positional):
                add_path(target, "read", index)
        elif name in _WRITE:
            targets = targets or (positional[:1] if selected == "powershell" and name != "new-item" else positional)
            if name == "new-item" and "name" in options:
                targets = [str(Path(target) / options["name"]) for target in targets]
            if not targets:
                unknown.append("写入目标无法静态确定。")
            for target in targets:
                add_path(target, "write", index)
        elif name in {"copy-item", "move-item", "cp", "mv"}:
            sources = targets or (positional if "destination" in options else positional[:-1])
            destination = options.get("destination") or (positional[-1] if positional else None)
            if not sources or destination is None:
                unknown.append("复制或移动目标无法静态确定。")
            source_paths = []
            for source in sources:
                source_path = add_path(source, "delete" if name in {"move-item", "mv"} else "read", index)
                source_paths.append((source, source_path))
                if source_path is not None and source_path.is_dir():
                    unknown.append("目录复制或移动涉及子项和链接，无法完整推断所有目标。")
            if "recurse" in options or any(
                value in {"--recursive", "--archive"} or (selected != "powershell" and value.startswith("-") and not value.startswith("--") and any(char in value for char in "rRa"))
                for value in item.argv[1:]
            ):
                unknown.append("递归复制或移动的完整目标需要审批。")
            if destination:
                destination_path = add_path(destination, "write", index)
                if destination_path is not None and destination_path.is_dir():
                    unknown.append("目标已是目录，需核对复制或移动生成的实际子路径。")
                    no_target_directory = "--no-target-directory" in options or "-T" in options
                    if not no_target_directory:
                        for source, source_path in source_paths:
                            leaf = Path(source).name
                            if source_path is not None and leaf:
                                add_path(str(Path(destination) / leaf), "write", index)
        elif name == "git":
            if not positional or positional[0].casefold() not in {"status", "diff", "log", "show", "rev-parse", "branch", "ls-files"}:
                unknown.append("Git 子命令可能修改文件或执行外部程序。")
            if len(item.argv) > 1 and item.argv[1].casefold() == "branch" and not _git_branch_is_listing(item.argv):
                unknown.append("Git branch 参数会修改引用或启动编辑器。")
            if any(value in {"-C", "--git-dir", "--work-tree", "-c", "--exec-path"} or value.startswith(("--git-dir=", "--work-tree=", "--exec-path=")) for value in item.argv[1:]):
                unknown.append("Git 参数改变执行目录、配置或执行程序。")
            if any(value in {"--ext-diff", "--textconv", "--output", "--delete", "-d", "-D", "-m", "-M", "-c", "-C"}
                   or value.startswith("--output=") for value in item.argv[1:]):
                unknown.append("Git 参数可能执行外部程序、写入文件或修改引用。")
            # 分支名称、查询模式及 commit 是引用，不是文件路径。
            git_path_values = () if len(item.argv) > 1 and item.argv[1].casefold() == "branch" else item.argv[1:]
            for value in git_path_values:
                if not value.startswith("-") and _looks_like_path(value):
                    add_path(value, "read", index)
        elif name not in _HARMLESS:
            unknown.append(f"{name or '命令'} 的文件影响不能静态确定。")
            for value in item.argv[1:]:
                if not value.startswith("-") and _looks_like_path(value):
                    add_path(value, "read", index)
        if name in _INTERPRETERS or (item.argv and _looks_like_path(item.argv[0])):
            unknown.append("命令运行解释器、脚本或显式路径程序，需要审批。")
        if item.argv and _looks_like_path(item.argv[0]):
            # 系统解释器和程序可安装在项目外；这是执行位置，不是数据读写授权。
            add_path(item.argv[0], "execute", index)

    for redirect in syntax.redirects:
        if redirect.raw is None:
            unknown.append("重定向目标需要运行时计算。")
            continue
        previous = [index for index, item in enumerate(ordered) if item.offset <= redirect.offset]
        index = previous[-1] if previous else 0
        current_cwd = command_cwds[index] if command_cwds else cwd.resolve()
        add_path(redirect.raw, "write" if redirect.write else "read", index)
    if not commands and not unknown:
        unknown.append("命令没有可静态检查的子命令。")
    return CommandAnalysis(command, selected, tuple(commands), tuple(paths), tuple(dict.fromkeys(unknown)), tuple(dict.fromkeys(denials)), tuple(deletes))
