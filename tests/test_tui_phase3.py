from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from lancher_code.models import ChatRequest, MessageUsage, StreamEvent, ToolDefinition, ToolExecutionResult
from lancher_code.session import SessionController
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.tui import ComposerTextArea, LanCherTextualApp
from lancher_code.turn_runner import TurnRunner

DelayedEvent = tuple[StreamEvent, float]


class EchoTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="echo_tool", description="echo", input_schema={"type": "object"})

    async def execute(self, arguments: dict[str, object], context) -> ToolExecutionResult:
        return ToolExecutionResult(call_id="", tool_name="echo_tool", ok=True, payload={"content": "ok"}, summary="ok")


class FakeProvider:
    def __init__(self, responses: list[list[StreamEvent | DelayedEvent] | Exception]) -> None:
        self._responses = responses
        self.requests: list[ChatRequest] = []

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        current = self._responses.pop(0)
        if isinstance(current, Exception):
            raise current
        for item in current:
            if isinstance(item, tuple):
                event, delay = item
            else:
                event, delay = item, 0.0
            yield event
            if delay > 0:
                await asyncio.sleep(delay)


def _build_app(provider: FakeProvider, provider_config, ui_config, tmp_path: Path) -> tuple[LanCherTextualApp, SessionController]:
    session = SessionController(provider_config, cwd=tmp_path, plan_file_path=Path("./.lancher/plan.md"))
    registry = ToolRegistry()
    registry.register(EchoTool())
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
    input_widget = app.query_one("#composer-input", ComposerTextArea)
    input_widget.text = value
    input_widget.focus()
    await pilot.press("enter")


@pytest.mark.asyncio
async def test_plan_command_switches_mode_and_updates_placeholder(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    app, session = _build_app(FakeProvider(responses=[]), openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "/plan")
        await pilot.pause(0.05)

        composer = app.query_one("#composer-input", ComposerTextArea)
        assert session.runtime_mode == "plan"
        assert "/do" in (composer.placeholder or "")


@pytest.mark.asyncio
async def test_plan_with_payload_submits_request_in_plan_mode(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="text_delta", text="计划已生成"),
                StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=1, output_tokens=1)),
            ]
        ]
    )
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "/plan 为搜索功能写计划")
        await pilot.pause(0.1)

        assert session.runtime_mode == "plan"
        assert len(provider.requests) == 1
        assert provider.requests[0].mode == "plan"


@pytest.mark.asyncio
async def test_do_command_restores_normal_mode(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    app, session = _build_app(FakeProvider(responses=[]), openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "/plan")
        await pilot.pause(0.05)
        await _submit_message(app, pilot, "/do")
        await pilot.pause(0.05)

        composer = app.query_one("#composer-input", ComposerTextArea)
        assert session.runtime_mode == "normal"
        assert "/plan" in (composer.placeholder or "")


@pytest.mark.asyncio
async def test_ctrl_c_cancels_active_turn_instead_of_exiting(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                (StreamEvent(kind="text_delta", text="先来一点"), 0.5),
                StreamEvent(kind="text_delta", text="后面的内容"),
                StreamEvent(kind="message_end"),
            ]
        ]
    )
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "请取消我")
        await pilot.pause(0.1)
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)

        assert session.state.messages[-1].status == "cancelled"
