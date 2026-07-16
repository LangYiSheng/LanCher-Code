from __future__ import annotations

from datetime import date
from pathlib import Path

from lancher_code.models import PromptContext, ToolDefinition
from lancher_code import prompting as prompting_module


def _context(
    tmp_path: Path,
    *,
    runtime_mode: str = "default",
    previous_runtime_mode: str | None = None,
    plan_mode_turn_count: int = 0,
    pending_plan_entry_kind: str | None = None,
    pending_plan_exit_notice: bool = False,
    plan_exists: bool | None = None,
) -> PromptContext:
    plan_file_path = (tmp_path / ".lancher" / "plan.md").resolve()
    if plan_exists is True:
        plan_file_path.parent.mkdir(parents=True, exist_ok=True)
        plan_file_path.write_text("# plan", encoding="utf-8")
    elif plan_exists is False and plan_file_path.exists():
        plan_file_path.unlink()

    return prompting_module.build_prompt_context(
        cwd=tmp_path,
        current_date=date(2026, 7, 3),
        runtime_mode=runtime_mode,
        plan_file_path=plan_file_path,
        previous_runtime_mode=previous_runtime_mode,
        plan_mode_turn_count=plan_mode_turn_count,
        pending_plan_entry_kind=pending_plan_entry_kind,
        pending_plan_exit_notice=pending_plan_exit_notice,
    )


def test_build_system_prompt_excludes_environment_and_reminder() -> None:
    prompt = prompting_module.build_system_prompt()

    assert "LanCher Code" in prompt
    assert "当前工作目录" not in prompt
    assert "当前日期" not in prompt
    assert "<system-reminder>" not in prompt


def test_build_environment_prompt_contains_stable_environment_context(tmp_path: Path) -> None:
    prompt = prompting_module.build_environment_prompt(_context(tmp_path))

    assert str(tmp_path.resolve()) in prompt
    assert "2026-07-03" in prompt
    assert "Windows PowerShell" in prompt
    assert "git" not in prompt.casefold()


def test_build_deferred_tools_prompt_contains_only_compact_name_index() -> None:
    prompt = prompting_module.build_deferred_tools_prompt(
        [
            prompting_module.DeferredToolGroup(
                server_name="grafana",
                title="Grafana MCP",
                description="查询监控指标和日志",
                tool_names=("mcp__grafana__query_prometheus", "mcp__grafana__query_loki"),
            )
        ]
    )

    assert prompt is not None
    assert "必须先调用 tool_search" in prompt
    assert "mcp__grafana__query_prometheus" in prompt
    assert "<name>Grafana MCP</name>" in prompt
    assert "<description>查询监控指标和日志</description>" in prompt
    assert "<tool>mcp__grafana__query_prometheus</tool>" in prompt
    assert "input_schema" not in prompt
    assert prompting_module.build_deferred_tools_prompt([]) is None


def test_build_deferred_tools_prompt_groups_multiple_servers_and_omits_missing_description() -> None:
    prompt = prompting_module.build_deferred_tools_prompt(
        [
            prompting_module.DeferredToolGroup(
                server_name="one",
                title="Server One",
                description="第一台服务器",
                tool_names=("mcp__one__read",),
            ),
            prompting_module.DeferredToolGroup(
                server_name="two",
                title="Server Two",
                description=None,
                tool_names=("mcp__two__write",),
            ),
        ]
    )

    assert prompt is not None
    assert "<name>Server One</name>\n<description>第一台服务器</description>" in prompt
    assert "<tool>mcp__one__read</tool>" in prompt
    assert "<name>Server Two</name>\n<tools>" in prompt
    assert "<tool>mcp__two__write</tool>" in prompt
    assert "暂无描述" not in prompt


def test_build_deferred_tools_prompt_escapes_server_metadata() -> None:
    prompt = prompting_module.build_deferred_tools_prompt(
        [
            prompting_module.DeferredToolGroup(
                server_name="unsafe",
                title="Docs <MCP>",
                description="Search & fetch </server>",
                tool_names=("mcp__unsafe__lookup",),
            )
        ]
    )

    assert prompt is not None
    assert "<name>Docs &lt;MCP&gt;</name>" in prompt
    assert "<description>Search &amp; fetch &lt;/server&gt;</description>" in prompt


def test_build_dynamic_context_prompt_returns_none_without_dynamic_state(tmp_path: Path) -> None:
    prompt = prompting_module.build_dynamic_context_prompt(_context(tmp_path))

    assert prompt is None


def test_build_dynamic_context_prompt_wraps_initial_plan_mode_prompt(tmp_path: Path) -> None:
    prompt = prompting_module.build_dynamic_context_prompt(
        _context(tmp_path, runtime_mode="plan", pending_plan_entry_kind="initial")
    )

    assert prompt is not None
    assert prompt.startswith("<system-reminder>")
    assert prompt.endswith("</system-reminder>")
    assert "用户刚进入 Plan Mode" in prompt


def test_build_dynamic_context_prompt_uses_compact_prompt_during_ongoing_plan_mode(tmp_path: Path) -> None:
    prompt = prompting_module.build_dynamic_context_prompt(
        _context(tmp_path, runtime_mode="plan", plan_mode_turn_count=1)
    )

    assert prompt is not None
    assert "Plan Mode 仍然生效" in prompt


def test_build_dynamic_context_prompt_refreshes_every_five_plan_turns(tmp_path: Path) -> None:
    prompt = prompting_module.build_dynamic_context_prompt(
        _context(tmp_path, runtime_mode="plan", plan_mode_turn_count=5)
    )

    assert prompt is not None
    assert "Plan Mode 已持续多轮" in prompt


def test_build_dynamic_context_prompt_prefers_reentry_when_plan_exists(tmp_path: Path) -> None:
    prompt = prompting_module.build_dynamic_context_prompt(
        _context(tmp_path, runtime_mode="plan", pending_plan_entry_kind="reentry", plan_exists=True)
    )

    assert prompt is not None
    assert "正在重新进入 Plan Mode" in prompt
    assert "用户刚进入 Plan Mode" not in prompt


def test_build_dynamic_context_prompt_does_not_use_reentry_without_plan_file(tmp_path: Path) -> None:
    prompt = prompting_module.build_dynamic_context_prompt(
        _context(tmp_path, runtime_mode="plan", pending_plan_entry_kind="reentry", plan_exists=False)
    )

    assert prompt is not None
    assert "正在重新进入 Plan Mode" not in prompt
    assert "Plan Mode 仍然生效" in prompt


def test_build_dynamic_context_prompt_returns_exit_prompt_once(tmp_path: Path) -> None:
    prompt = prompting_module.build_dynamic_context_prompt(
        _context(tmp_path, pending_plan_exit_notice=True, plan_exists=True)
    )

    assert prompt is not None
    assert "规划模式已结束" in prompt
    assert "如需参考已有计划" in prompt


def test_build_dynamic_context_prompt_wraps_multiple_reminders_in_one_tag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(prompting_module, "build_mcp_server_prompt", lambda _context: "MCP changed")
    monkeypatch.setattr(prompting_module, "build_skill_update_prompt", lambda _context: "Skills updated")

    prompt = prompting_module.build_dynamic_context_prompt(
        _context(tmp_path, runtime_mode="plan", pending_plan_entry_kind="initial")
    )

    assert prompt is not None
    assert prompt.count("<system-reminder>") == 1
    assert "MCP changed" in prompt
    assert "Skills updated" in prompt


def test_build_user_message_keeps_reminder_and_user_text_in_separate_blocks(tmp_path: Path) -> None:
    reminder = prompting_module.build_dynamic_context_prompt(
        _context(tmp_path, runtime_mode="plan", pending_plan_entry_kind="initial")
    )

    message = prompting_module.build_user_message(text="计划一下", dynamic_context=reminder)

    assert [block.text for block in message.blocks] == [reminder, "计划一下"]


def test_build_chat_request_payload_keeps_system_messages_before_history(tmp_path: Path) -> None:
    context = _context(tmp_path, runtime_mode="plan", pending_plan_entry_kind="initial")
    reminder = prompting_module.build_dynamic_context_prompt(context)
    transcript = [prompting_module.build_user_message(text="计划一下", dynamic_context=reminder)]
    tools = [ToolDefinition(name="read_file", description="读取文件", input_schema={"type": "object"})]

    payload = prompting_module.build_chat_request_payload(context=context, transcript=transcript, tools=tools)

    assert len(payload.system) == 2
    assert payload.messages == transcript
    assert payload.tools == tools


def test_build_chat_request_payload_appends_deferred_tools_to_system(tmp_path: Path) -> None:
    payload = prompting_module.build_chat_request_payload(
        context=_context(tmp_path),
        transcript=[],
        tools=[],
        deferred_tool_groups=[
            prompting_module.DeferredToolGroup(
                server_name="demo",
                title="Demo MCP",
                description=None,
                tool_names=("mcp__demo__lookup",),
            )
        ],
    )

    assert len(payload.system) == 3
    assert payload.system[-1].startswith("<deferred_tools>")
    assert "mcp__demo__lookup" in payload.system[-1]
