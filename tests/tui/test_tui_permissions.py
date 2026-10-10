from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
import json

import pytest
from tui_test_support import complete_response_script
from lancher_code.contracts.messages import ChatRequest, StreamEvent
from lancher_code.contracts.tools import ToolCallChunk
from lancher_code.permissions.models import PermissionRequest
from lancher_code.sessions.controller import SessionController
from lancher_code.permissions.engine import PermissionEngine
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.tools.builtin.command import RunCommandTool
from lancher_code.execution import processes as process_module
import os
from lancher_code.tools.builtin.write_file import WriteFileTool
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.tui.permission import InlinePermissionPanel
from lancher_code.tui.app import LanCherTextualApp
from lancher_code.tui.permission import PermissionOption
from lancher_code.agent.runner import TurnRunner


class FakeProvider:
    def __init__(self, responses: list[list[StreamEvent]]) -> None:
        self._responses = [complete_response_script(response) for response in responses]
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
        permission_storage=permission_storage,
    )
    registry = ToolRegistry()
    registry.register(RunCommandTool())
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
            StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-1", name_delta="run_command")),
            StreamEvent(
                kind="tool_call_delta",
                tool_call_chunk=ToolCallChunk(
                    call_index=0,
                    arguments_delta='{"description":"查看 git 状态","command":"git status"}',
                ),
            ),
            StreamEvent(kind="message_end", response_complete=True),
        ],
        [
            StreamEvent(kind="message_start"),
            StreamEvent(kind="text_delta", text="已改用无需执行命令的策略"),
            StreamEvent(kind="message_end", response_complete=True),
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
            StreamEvent(kind="message_end", response_complete=True),
        ],
        [
            StreamEvent(kind="message_start"),
            StreamEvent(kind="text_delta", text="已停止文件写入"),
            StreamEvent(kind="message_end", response_complete=True),
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
        assert len(panel._options()) == 2
        assert options[0].has_class("-active")
        assert not options[2].display
        await pilot.press("m")
        await pilot.pause(0.05)
        assert len(panel._options()) == 4
        assert "RunCommand(git status)" in options[2].render().plain
        assert "RunCommand(git status)" in options[3].render().plain

        await pilot.press("tab")
        await pilot.pause(0.05)
        assert options[1].has_class("-active")

        await pilot.press("shift+tab")
        await pilot.pause(0.05)
        assert options[0].has_class("-active")

        await pilot.press("up")
        await pilot.pause(0.05)
        assert options[3].has_class("-active")

        await pilot.press("escape")
        await pilot.pause(0.2)

        assert session.state.messages[-1].status == "complete"
        assert session.state.messages[-1].content == "已改用无需执行命令的策略"


@pytest.mark.asyncio
async def test_process_input_permission_never_offers_permanent_scope(openai_provider_config, ui_config, tmp_path):
    app, _ = _build_app(FakeProvider([]), openai_provider_config, ui_config, tmp_path)
    request = PermissionRequest("input-request", "input-call", "process_write", "ProcessWrite", "command",
                                "发送进程输入", "这可能执行新的命令", "待发送正文", command="npm run build",
                                metadata={"allow_once_only": True})
    async with app.run_test(size=(100, 40)) as pilot:
        await app._request_inline_permission(request)
        await pilot.pause()
        panel = app.query_one(InlinePermissionPanel)
        assert [item.outcome for item in panel.query(PermissionOption)] == ["allow_once", "deny"]
        await pilot.press("m")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        panel.set_compact(True)
        await pilot.press("m")
        await pilot.pause()
        assert len(app.screen_stack) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key_presses", "expected_outcome"),
    [
        ([], "allow_once"),
        (["m", "down", "down"], "allow_session"),
        (["m", "down", "down", "down"], "allow_project"),
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
async def test_allow_session_resolution_is_persisted_with_automatic_session(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(responses=_permission_request_responses())
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "查看仓库状态")
        await pilot.pause(0.1)
        await pilot.press("m", "down", "down")
        await pilot.press("enter")
        await pilot.pause(1.5)

    saved_id = session.session_id
    assert saved_id is not None
    session.close()
    restored = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        assert restored.resume_session(saved_id) == 1
        assert restored._permission_storage.rules_for_scope("session")[0].match == "RunCommand(git status)"
        assert restored._permission_storage.rules_for_scope("session")[0].match_kind == "exact"
    finally:
        restored.close()


@pytest.mark.skipif(os.name != "nt", reason="此 UI 收尾测试使用 Windows PowerShell 命令")
@pytest.mark.asyncio
async def test_closing_ui_waits_for_running_command_cleanup(openai_provider_config, ui_config, tmp_path, monkeypatch):
    responses = _permission_request_responses()
    responses[0][2].tool_call_chunk.arguments_delta = json.dumps({
        "description": "界面退出收尾测试", "command": "Start-Sleep -Seconds 30",
    })
    app, _ = _build_app(FakeProvider(responses), openai_provider_config, ui_config, tmp_path)
    app._turn_runner.set_permission_policy("bypass")
    original_spawn = process_module.spawn_backend
    started = asyncio.Event()
    processes = []

    async def capture_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(process_module, "spawn_backend", capture_spawn)
    try:
        async with app.run_test() as pilot:
            await _submit_message(app, pilot, "启动命令")
            await asyncio.wait_for(started.wait(), 5)
            assert app._turn_runner.has_active_turn
        assert not app._turn_runner.has_active_turn
        assert processes
        for process in processes:
            assert await asyncio.wait_for(process.wait(), 5) is not None
    finally:
        for process in processes:
            await process.terminate()
            await process.close()
