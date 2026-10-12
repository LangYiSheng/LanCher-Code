from __future__ import annotations

import ast
import os
from importlib.util import resolve_name
from pathlib import Path
import subprocess
import sys
import textwrap


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "lancher_code"
UI_MODULES = ("rich", "textual", "lancher_code.tui", "lancher_code.app")


def _is_ui_module(module: str) -> bool:
    return any(module == name or module.startswith(name + ".") for name in UI_MODULES)


def test_core_has_no_ui_imports() -> None:
    """静态约束覆盖所有核心域，防止未执行的功能分支重新依赖界面。"""
    violations: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT)
        if relative.parts[0] == "tui" or relative.as_posix() in {"app.py", "cli.py", "__main__.py"}:
            continue
        package = ".".join(("lancher_code", *relative.parts[:-1]))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = resolve_name("." * node.level + module, package)
                imported = [module, *(module + "." + alias.name for alias in node.names)]
            elif (
                isinstance(node, ast.Call)
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and (
                    isinstance(node.func, ast.Name) and node.func.id == "__import__"
                    or isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
                )
            ):
                imported = [node.args[0].value]
            for module in imported:
                if _is_ui_module(module):
                    violations.append(f"{relative}:{node.lineno}: {module}")
    assert not violations, "核心不得依赖界面模块：\n" + "\n".join(violations)


def _run_without_ui(code: str) -> subprocess.CompletedProcess[str]:
    """独立解释器拒绝加载界面依赖，避免 pytest 已加载的模块掩盖耦合。"""
    script = textwrap.dedent(
        """
        import importlib.abc
        import sys

        blocked = ("rich", "textual", "lancher_code.tui", "lancher_code.app")

        class NoUIImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
                    raise ModuleNotFoundError("界面依赖已禁用：" + fullname, name=fullname)
                return None

        sys.meta_path.insert(0, NoUIImports())
        """
    ) + "\n" + textwrap.dedent(code)
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


def test_public_sdk_imports_without_ui_dependencies() -> None:
    result = _run_without_ui(
        """
        from lancher_code.sdk import (
            AgentConfig, AgentEvent, AgentResult, AgentService,
            PermissionRequest, PermissionResolution, create_agent,
        )

        assert callable(create_agent)
        assert not any(
            name == prefix or name.startswith(prefix + ".")
            for name in sys.modules for prefix in blocked
        )
        """
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cli_help_does_not_load_ui_dependencies() -> None:
    result = _run_without_ui(
        """
        from lancher_code.cli import main

        try:
            main(["--help"])
        except SystemExit as exc:
            assert exc.code == 0
        else:
            raise AssertionError("帮助命令应结束而不启动应用")

        assert not any(
            name == prefix or name.startswith(prefix + ".")
            for name in sys.modules for prefix in blocked
        )
        """
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--help" in result.stdout
