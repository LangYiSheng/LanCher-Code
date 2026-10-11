from __future__ import annotations

import os
from pathlib import Path

import pytest

from lancher_code.permissions import shell as module
from lancher_code.permissions.shell import MAX_COMMAND_BYTES, analyze_command


def analyze(command: str, root: Path, shell: str = "bash"):
    return analyze_command(command, shell=shell, cwd=root, project_root=root)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "Sub Dir").mkdir()
    return tmp_path


def test_bash_compound_commands_preserve_original_text_and_argument_case(project):
    command = 'git status\ncat "Mixed Case.txt" | head\necho "Keep  Spaces" && rm -- "Sub Dir/File.TXT"'
    result = analyze(command, project)
    assert result.raw_command == command
    assert [item.name for item in result.commands] == ["git", "cat", "head", "echo", "rm"]
    assert result.commands[1].argv == ("cat", "Mixed Case.txt")
    assert result.commands[3].argv == ("echo", "Keep  Spaces")
    assert result.deletes[0].path == project / "Sub Dir/File.TXT"
    assert not result.hard_denials


def test_bash_directory_changes_and_redirects_have_execution_cwd(project):
    result = analyze('cd "Sub Dir" && cat In.txt; echo Done > Out.txt', project)
    assert result.commands[1].cwd == project / "Sub Dir"
    assert result.unknown_reasons
    assert {(item.operation, item.path) for item in result.path_accesses} == {
        ("cwd", project / "Sub Dir"), ("read", project / "Sub Dir/In.txt"), ("write", project / "Sub Dir/Out.txt"),
        ("read", project / "In.txt"), ("write", project / "Out.txt")}


def test_bash_read_redirect_and_fd_duplication(project):
    result = analyze("cat < In.txt 2>&1", project)
    assert [(item.raw, item.operation) for item in result.path_accesses] == [("In.txt", "read")]
    assert result.commands[0].raw == "cat < In.txt 2>&1"


@pytest.mark.parametrize("command", [
    'rm "$TARGET"', 'echo $(rm file.txt)', 'for x in file.txt; do rm "$x"; done',
    'sh -c "rm file.txt"', 'python -c "print(1)"', 'source setup.sh',
    'rm *.txt', 'if true; then cd "Sub Dir"; fi; rm file.txt', 'echo `pwd`',
    'cd "Sub Dir" || rm file.txt', 'cd "Sub Dir" | echo done; rm file.txt', 'rm a{b,c}', 'rm file.txt &',
])
def test_bash_dynamic_or_script_commands_are_unknown(command, project):
    assert analyze(command, project).unknown_reasons


def test_bash_nested_substitution_still_exposes_delete(project):
    result = analyze('echo $(rm file.txt)', project)
    assert [item.name for item in result.commands] == ["echo", "rm"]
    assert result.deletes[0].path == project / "file.txt"


@pytest.mark.parametrize("command", ["format volume", "sudo mkfs.ext4 /dev/sda", "dd if=file of=/dev/sda", "wipefs -a /dev/sda", "sh -c 'mkfs /dev/sda'"])
def test_high_risk_operations_are_ast_commands(command, project):
    assert analyze(command, project).hard_denials


def test_quoted_risk_words_are_not_commands(project):
    result = analyze('echo "format > sudo rm"', project)
    assert result.complete
    assert not result.hard_denials
    assert not result.path_accesses


def test_git_dry_run_filter_is_not_a_force_option(project):
    assert not analyze("git clean --dry-run --exclude=foo", project).hard_denials


@pytest.mark.parametrize("command", ["git diff --ext-diff", "git log --output=Log.txt", "git branch -D old", "RM file", "rm.exe file", "npm run build", "make test"])
def test_external_program_effects_cannot_be_automatically_inferred(command, project):
    assert analyze(command, project).unknown_reasons


def test_long_command_is_not_truncated_before_tail_delete(project):
    command = 'echo "' + 'a' * 16_000 + '"\nrm Late.txt'
    result = analyze(command, project)
    assert result.raw_command == command
    assert result.deletes[0].path == project / "Late.txt"


def test_over_budget_and_parse_error_are_unknown(project):
    assert analyze('echo "' + 'a' * MAX_COMMAND_BYTES + '"', project).unknown_reasons
    assert analyze('echo "unterminated', project).unknown_reasons


def test_parser_unavailability_is_unknown(monkeypatch, project):
    monkeypatch.setattr(module, "_powershell_executable", lambda: None)
    result = analyze_command("Echo UniqueParserUnavailable", shell="powershell", cwd=project, project_root=project)
    assert result.unknown_reasons


def test_path_resolution_is_repeated_even_when_syntax_is_cached(project):
    result = analyze("cat file.txt", project)
    other = project / "Sub Dir"
    second = analyze("cat file.txt", other)
    assert result.path_accesses[0].path == project / "file.txt"
    assert second.path_accesses[0].path == other / "file.txt"


def test_rmdir_parent_deletions_are_explicit(project):
    result = analyze("rmdir -p Sub/Child", project)
    assert [item.path for item in result.deletes] == [project / "Sub/Child", project / "Sub"]


@pytest.mark.parametrize("command", ["cp -t Destination One.txt Two.txt", "cp --target-directory=Destination One.txt Two.txt"])
def test_copy_target_directory_is_checked_as_write(command, project):
    result = analyze(command, project)
    assert [(item.raw, item.operation) for item in result.path_accesses] == [
        ("One.txt", "read"), ("Two.txt", "read"), ("Destination", "write")]


@pytest.mark.skipif(os.name != "nt", reason="验证与 Windows 执行后端一致的 PowerShell AST")
class TestPowerShell:
    def test_literals_aliases_and_newline_delete(self, project):
        command = 'Write-Output "format > sudo"\nrm -LiteralPath "Mixed Case.txt"'
        result = analyze(command, project, "powershell")
        assert result.raw_command == command
        assert result.commands[1].argv == ("rm", "-LiteralPath", "Mixed Case.txt")
        assert result.deletes[0].path == project / "Mixed Case.txt"
        assert not result.hard_denials
        assert result.complete

    def test_directory_change_and_write_target(self, project):
        result = analyze('Set-Location "Sub Dir"; Set-Content -LiteralPath Out.txt -Value "data > format"', project, "powershell")
        assert result.unknown_reasons
        assert any(item.path == project / "Sub Dir/Out.txt" and item.operation == "write" for item in result.path_accesses)
        assert any(item.path == project / "Out.txt" and item.operation == "write" for item in result.path_accesses)

    def test_recursive_delete_is_visible(self, project):
        result = analyze('Remove-Item -Path "Sub Dir" -Recurse -Force', project, "powershell")
        assert result.deletes[0].recursive
        assert not result.hard_denials

    def test_multiple_delete_paths_are_complete(self, project):
        result = analyze('Remove-Item -Path One.txt,Two.txt', project, "powershell")
        assert result.complete
        assert [item.path for item in result.deletes] == [project / "One.txt", project / "Two.txt"]

    @pytest.mark.parametrize("command", ['Set-Content -Path One.txt,Two.txt -Value Data', 'New-Item -Path One.txt,Two.txt -ItemType File'])
    def test_multiple_write_path_parameters(self, command, project):
        result = analyze(command, project, "powershell")
        assert [item.path for item in result.path_accesses] == [project / "One.txt", project / "Two.txt"]

    def test_new_item_name_is_part_of_write_target(self, project):
        result = analyze('New-Item -Path "Sub Dir" -Name ../Out.txt -ItemType File', project, "powershell")
        assert result.path_accesses[0].path == project / "Out.txt"

    def test_filter_does_not_imply_recursive(self, project):
        result = analyze('Remove-Item -Path "Sub Dir" -Filter *.txt', project, "powershell")
        assert result.unknown_reasons
        assert not result.deletes[0].recursive

    def test_redirection_preserves_literal_and_detects_path(self, project):
        result = analyze('Write-Output "a > b" > Out.txt', project, "powershell")
        assert result.complete
        assert result.path_accesses[0].path == project / "Out.txt"
        assert result.path_accesses[0].operation == "write"

    @pytest.mark.parametrize("command", [
        'Remove-Item $path', 'Get-ChildItem | Remove-Item',
        '& { Remove-Item file.txt }', 'foreach ($x in @(1)) { Remove-Item file.txt }',
        'powershell -Command "Remove-Item file.txt"',
        'python -c "print(1)"', 'Remove-Item *.txt',
        'Set-Location $path; Remove-Item file.txt',
    ])
    def test_dynamic_execution_is_recorded_as_unknown(self, command, project):
        assert analyze(command, project, "powershell").unknown_reasons

    def test_script_block_delete_is_still_visible(self, project):
        result = analyze('& { Remove-Item file.txt }', project, "powershell")
        assert result.deletes[0].path == project / "file.txt"

    @pytest.mark.parametrize("command", [
        "Write-Output x; [System.IO.File]::Delete('../outside.txt')",
        "Write-Output x; (Get-Item One.txt).Delete()",
        "Write-Output x; 1 + 1",
        "Write-Output x; [System.Environment]::Exit(0)",
    ])
    def test_mixed_commands_and_unclassified_expressions_are_unknown(self, command, project):
        result = analyze(command, project, "powershell")
        assert result.commands[0].name == "write-output"
        assert result.unknown_reasons

    def test_parse_never_executes_source(self, project):
        sentinel = project / "must-not-exist.txt"
        result = analyze(f'Write-Output "payload" > "{sentinel}"', project, "powershell")
        assert result.path_accesses[0].path == sentinel
        assert not sentinel.exists()


@pytest.mark.parametrize("shell,command", [
    ("bash", "cd missing/deep; rm ../../file.txt"),
    ("powershell", "Set-Location missing/deep; Remove-Item -LiteralPath ../../file.txt"),
])
def test_failed_directory_change_does_not_imply_safe_execution(shell, command, project):
    result = analyze(command, project, shell)
    assert result.unknown_reasons


def test_explicit_program_path_is_exposed_to_scope_validation(project):
    result = analyze("../Outside/tool.sh file.txt", project)
    assert result.unknown_reasons
    assert result.path_accesses[0].path == (project / "../Outside/tool.sh").resolve()
    assert result.path_accesses[0].operation == "execute"


@pytest.mark.parametrize("shell,command", [("bash", "sleep 0.01"), ("powershell", "Start-Sleep -Milliseconds 10")])
def test_static_wait_has_no_file_effect(shell, command, project):
    result = analyze(command, project, shell)
    assert result.complete
    assert not result.path_accesses


def test_external_interpreter_location_is_execute_but_script_remains_unknown(project):
    program = project.parent / "Python/python.exe"
    command = f'& "{program}" -c "print(1)"'
    result = analyze(command, project, "powershell")
    assert result.unknown_reasons
    assert result.path_accesses[0].path == program
    assert result.path_accesses[0].operation == "execute"


def test_external_high_risk_program_is_still_denied(project):
    program = project.parent / "System/format.exe"
    result = analyze(f'& "{program}" volume', project, "powershell")
    assert result.hard_denials


@pytest.mark.parametrize("command", [
    "git branch new-branch", "git branch --track new main", "git branch --set-upstream-to=origin/main main",
    "git branch --unset-upstream main", "git branch --edit-description", "git branch --all new-branch",
])
def test_git_branch_mutations_are_unknown(command, project):
    assert analyze(command, project).unknown_reasons


@pytest.mark.parametrize("command", ["git branch", "git branch --all", "git branch -vv", "git branch --list 'feature/*'", "git branch --contains HEAD", "git branch --show-current"])
def test_git_branch_listing_is_recognized(command, project):
    assert analyze(command, project).complete


@pytest.mark.parametrize("shell,command", [
    ("bash", "cp -R .git Destination"),
    ("powershell", "Copy-Item -LiteralPath .git -Destination Destination -Recurse -Force"),
])
def test_directory_copy_exposes_implicit_metadata_target(shell, command, project):
    (project / ".git").mkdir()
    (project / "Destination").mkdir()
    result = analyze(command, project, shell)
    assert result.unknown_reasons
    assert any(item.path == project / "Destination/.git" and item.operation == "write" for item in result.path_accesses)


def test_copy_into_existing_directory_exposes_actual_child(project):
    (project / "Destination").mkdir()
    result = analyze("cp One.txt Destination", project)
    assert result.unknown_reasons
    assert result.path_accesses[-1].path == project / "Destination/One.txt"


def test_recursive_copy_without_existing_destination_is_unknown(project):
    assert analyze("cp -R Source Destination", project).unknown_reasons


@pytest.mark.parametrize("command", ["sudo rm file", "shutdown now", "git reset --hard", "git clean -fdx", "git -C . reset --hard", "git clean --force", "git checkout -- a.txt", "git restore --source=HEAD a.txt"])
def test_ordinary_destructive_operations_are_not_hard_denied(command, project):
    assert not analyze(command, project).hard_denials


@pytest.mark.parametrize("command,expected", [("cat file.txt", True), ("echo ready", True), ("git status", True), ("rm file.txt", False), ("touch file.txt", False), ("npm run build", False), ("python -c 'print(1)'", False), ("git reset --hard", False)])
def test_read_only_requires_complete_query(command, expected, project):
    assert analyze(command, project).read_only is expected


def test_git_external_directory_and_output_paths_are_access_facts(project):
    external = project.parent / "external"
    result = analyze(f'git -C "{external}" log --output=Log.txt', project)
    assert any(item.operation == "cwd" and item.path == external for item in result.path_accesses)
    assert any(item.operation == "write" and item.path == external / "Log.txt" for item in result.path_accesses)


def test_failed_directory_change_keeps_original_cwd_candidate(project):
    result = analyze('cd missing/deep; rm ../../external.txt', project)
    assert any(item.path == (project / '../../external.txt').resolve() for item in result.deletes)


def test_unknown_program_named_output_path_is_detected(project):
    result = analyze('generator --output=../external.txt', project)
    assert any(item.operation == "write" and item.path == project.parent / "external.txt" for item in result.path_accesses)


def test_nested_literal_shell_exposes_external_write(project):
    result = analyze("sh -c 'echo data > ../external.txt'", project)
    assert any(item.operation == "write" and item.path == project.parent / "external.txt" for item in result.path_accesses)


def test_inline_python_source_is_not_a_literal_file_path(project):
    result = analyze('python -c "print(1 / 2)"', project)
    assert result.unknown_reasons
    assert not result.path_accesses


def test_delete_symlink_preserves_the_link_target(project):
    link = project / "link.txt"
    try:
        link.symlink_to(project.parent / "external.txt")
    except OSError:
        pytest.skip("当前系统不允许创建符号链接")
    result = analyze("rm link.txt", project)
    assert result.deletes[0].path == link


@pytest.mark.skipif(os.name != "nt", reason="需要 PowerShell AST")
def test_encoded_powershell_risk_is_decoded_without_execution(project):
    import base64
    source = "Format-Volume -DriveLetter X"
    encoded = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
    assert analyze(f'powershell -EncodedCommand {encoded}', project, "powershell").hard_denials


@pytest.mark.parametrize("command", ['curl http://127.0.0.1:8080/', 'curl https://example.com/path', 'git clone git@github.com:org/repo.git'])
def test_remote_urls_are_not_local_write_paths(command, project):
    assert not analyze(command, project).path_accesses


def test_file_url_is_still_a_local_access_path(project):
    target = project.parent / "external.txt"
    result = analyze(f'curl "{target.as_uri()}"', project)
    assert any(item.path == target and item.operation == "write" for item in result.path_accesses)


def test_nested_windows_disk_tool_is_detected_from_bash(project):
    assert analyze("sh -c 'format.exe X:'", project).hard_denials
