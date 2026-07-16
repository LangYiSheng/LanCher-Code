from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
import json

import pytest
from lancher_code.models import ChatRequest, StreamEvent, ToolCallChunk
from lancher_code.session import SessionController
from lancher_code.permission_engine import PermissionEngine, PermissionStorage
from lancher_code.tools.builtin.bash import BashTool
from lancher_code.tools.builtin.write_file import WriteFileTool
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.tui import InlinePermissionPanel, LanCherTextualApp
from lancher_code.tui_views.permission import PermissionOption
from lancher_code.turn_runner import TurnRunner


class FakeProvider:
    def __init__(self, responses: list[list[StreamEvent]]) -> None:
        self._responses = responses
        self.requests: list[ChatRequest] = []

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        for event in self._responses.pop(0):
            yield event


def _build_app(provider: FakeProvider, provider_config, ui_config, tmp_path: Path) -> tuple[LanCherTextualApp, SessionController]:
    permission_storage = PermissionStorage()
    session = SessionController(
        provider_config,
        cwd=tmp_path,
        plan_file_path=Path("./.lancher/plan.md"),
        permission_storage=permission_storage,
    )
    registry = ToolRegistry()
    registry.register(BashTool())
    registry.register(WriteFileTool())
    executor = ToolExecutor(
        registry,
        cwd=tmp_path,
        timeout_seconds=1,
        permission_engine=PermissionEngine(permission_storage),
    )
    runner = TurnRunner(provider, session, registry, executor)
    app = LanCherTextualApp(
        turn_runner=runner,
        provider_config=provider_config,
        session_controller=session,
        ui_config=ui_config,
    )
    return app, session


async def _submit_message(app: LanCherTextualApp, pilot, value: str) -> None:
    composer = app.query_one("#composer-input")
    composer.text = value
    composer.focus()
    await pilot.press("enter")


def _permission_request_responses() -> list[list[StreamEvent]]:
    return [
        [
            StreamEvent(kind="message_start"),
            StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-1", name_delta="bash")),
            StreamEvent(
                kind="tool_call_delta",
                tool_call_chunk=ToolCallChunk(
                    call_index=0,
                    arguments_delta='{"description":"查看 git 状态","command":"git status"}',
                ),
            ),
            StreamEvent(kind="message_end"),
        ],
        [
            StreamEvent(kind="message_start"),
            StreamEvent(kind="text_delta", text="已改用无需执行命令的策略"),
            StreamEvent(kind="message_end"),
        ],
    ]


def _file_permission_request_responses() -> list[list[StreamEvent]]:
    return [
        [
            StreamEvent(kind="message_start"),
            StreamEvent(
                kind="tool_call_delta",
                tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-file", name_delta="write_file"),
            ),
            StreamEvent(
                kind="tool_call_delta",
                tool_call_chunk=ToolCallChunk(
                    call_index=0,
                    arguments_delta='{"path":"demo.txt","content":"hello"}',
                ),
            ),
            StreamEvent(kind="message_end"),
        ],
        [
            StreamEvent(kind="message_start"),
            StreamEvent(kind="text_delta", text="已停止文件写入"),
            StreamEvent(kind="message_end"),
        ],
    ]


@pytest.mark.asyncio
async def test_permission_denial_does_not_break_turn(openai_provider_config, ui_config, tmp_path: Path) -> None:
    provider = FakeProvider(responses=_permission_request_responses())
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "看看仓库状态")
        await pilot.pause(0.1)

        assert isinstance(app.query_one(InlinePermissionPanel), InlinePermissionPanel)
        await pilot.press("escape")
        await pilot.pause(0.2)

        assert session.state.messages[-1].status == "complete"
        assert session.state.messages[-1].content == "已改用无需执行命令的策略"
        assert any(entry.kind == "tool_result" and entry.ok is False for entry in session.state.messages[-1].trace.entries)


@pytest.mark.asyncio
async def test_file_edit_permission_uses_inline_panel(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(responses=_file_permission_request_responses())
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "写入 demo.txt")
        await pilot.pause(0.1)

        panel = app.query_one(InlinePermissionPanel)
        assert panel.request.kind == "file_edit"
        assert "demo.txt" in panel.query_one("#permission-details").render().plain
        assert len(list(panel.query(PermissionOption))) == 2

        await pilot.press("escape")
        await pilot.pause(0.2)

        assert session.state.messages[-1].status == "complete"
        assert session.state.messages[-1].content == "已停止文件写入"


@pytest.mark.asyncio
async def test_command_permission_panel_supports_keyboard_navigation_and_shows_rules(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(responses=_permission_request_responses())
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "看看仓库状态")
        await pilot.pause(0.1)

        panel = app.query_one(InlinePermissionPanel)
        options = list(panel.query(PermissionOption))
        assert app.focused is panel
        assert len(options) == 4
        assert options[0].has_class("-active")
        assert "Bash(git *)" in options[1].render().plain
        assert "Bash(git *)" in options[2].render().plain

        await pilot.press("tab")
        await pilot.pause(0.05)
        assert options[1].has_class("-active")

        await pilot.press("shift+tab")
        await pilot.pause(0.05)
        assert options[0].has_class("-active")

        await pilot.press("up")
        await pilot.pause(0.05)
        assert options[3].has_class("-active")

        await pilot.press("enter")
        await pilot.pause(0.2)

        assert session.state.messages[-1].status == "complete"
        assert session.state.messages[-1].content == "已改用无需执行命令的策略"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key_presses", "expected_outcome"),
    [
        ([], "allow_once"),
        (["down"], "allow_session"),
        (["down", "down"], "allow_project"),
        (["up"], "deny"),
    ],
)
async def test_command_permission_panel_returns_each_outcome(
    key_presses: list[str],
    expected_outcome: str,
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(responses=_permission_request_responses())
    app, _session = _build_app(provider, openai_provider_config, ui_config, tmp_path)
    captured_outcomes: list[str] = []
    original_resolve = app._turn_runner.resolve_permission_request

    def capture_resolution(resolution) -> bool:
        captured_outcomes.append(resolution.outcome)
        return original_resolve(resolution)

    app._turn_runner.resolve_permission_request = capture_resolution

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "看看仓库状态")
        await pilot.pause(0.1)
        for key in key_presses:
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause(0.2)

    assert captured_outcomes == [expected_outcome]


@pytest.mark.asyncio
async def test_allow_session_resolution_is_auto_saved_with_bound_session(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(responses=_permission_request_responses())
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)
    session.save_session("permission-session")

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "查看仓库状态")
        await pilot.pause(0.1)
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause(1.5)

    path = tmp_path / ".lancher" / "session" / "permission-session.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    permissions = next(record for record in records if record["type"] == "permissions")
    assert permissions["data"]["rules"] == [
        {"match": "Bash(git *)", "result": "allow"}
    ]
