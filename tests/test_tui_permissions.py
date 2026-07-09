from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from textual.widgets import Button

from lancher_code.models import ChatRequest, StreamEvent, ToolCallChunk
from lancher_code.session import SessionController
from lancher_code.tools.builtin.bash import BashTool
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.tui import LanCherTextualApp, PermissionRequestScreen
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
    session = SessionController(provider_config, cwd=tmp_path, plan_file_path=Path("./.lancher/plan.md"))
    registry = ToolRegistry()
    registry.register(BashTool())
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=1)
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


@pytest.mark.asyncio
async def test_permission_denial_does_not_break_turn(openai_provider_config, ui_config, tmp_path: Path) -> None:
    provider = FakeProvider(responses=_permission_request_responses())
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "看看仓库状态")
        await pilot.pause(0.1)

        deny_button = app.screen.query_one("#permission-deny", Button)
        deny_button.press()
        await pilot.pause(0.2)

        assert session.state.messages[-1].status == "complete"
        assert session.state.messages[-1].content == "已改用无需执行命令的策略"
        assert any(entry.kind == "tool_result" and entry.ok is False for entry in session.state.messages[-1].trace.entries)


@pytest.mark.asyncio
async def test_permission_request_screen_supports_keyboard_navigation(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(responses=_permission_request_responses())
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "看看仓库状态")
        await pilot.pause(0.1)

        assert isinstance(app.screen, PermissionRequestScreen)
        assert isinstance(app.screen.focused, Button)
        assert app.screen.focused.id == "permission-allow_once"

        await pilot.press("tab")
        await pilot.pause(0.05)
        assert isinstance(app.screen.focused, Button)
        assert app.screen.focused.id == "permission-allow_session"

        await pilot.press("shift+tab")
        await pilot.pause(0.05)
        assert isinstance(app.screen.focused, Button)
        assert app.screen.focused.id == "permission-allow_once"

        await pilot.press("left")
        await pilot.pause(0.05)
        assert isinstance(app.screen.focused, Button)
        assert app.screen.focused.id == "permission-deny"

        await pilot.press("enter")
        await pilot.pause(0.2)

        assert session.state.messages[-1].status == "complete"
        assert session.state.messages[-1].content == "已改用无需执行命令的策略"
