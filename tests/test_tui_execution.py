"""真实 run_async、审批与原生进程；覆盖测试驱动器和实际启动路径的差别。"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import socket
import sys

import httpx
import pytest

from lancher_code.execution.contracts import CommandProfile, ExecutionConfig, ReadinessProbe, ResourceClaim
from lancher_code.execution.runtime import ExecutionRuntime
from lancher_code.models import StreamEvent, ToolCallChunk, UIConfig
from lancher_code.permission_engine import PermissionEngine, PermissionStorage
from lancher_code.session import SessionController
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tui_views.chat import LanCherTextualApp
from lancher_code.tui_views.composer import ComposerTextArea
from lancher_code.tui_views.permission import InlinePermissionPanel
from lancher_code.tui_views.tasks import TasksScreen
from lancher_code.turn_runner import TurnRunner


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = list(responses)

    async def stream_chat(self, request):
        # 实际网络流会挂起；轮次身份必须在请求前就已经确定。
        await asyncio.sleep(0)
        for event in self.responses.pop(0):
            yield event


def reply():
    return [StreamEvent(kind="text_delta", text="完成"), StreamEvent(kind="message_end")]


def command_call(command, **options):
    arguments = {"command": command, "description": "真实界面进程回归", "max_runtime_ms": 30000, **options}
    return [StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(
        call_index=0, provider_call_id="provider-call", name_delta="run_command",
        arguments_delta=json.dumps(arguments))), StreamEvent(kind="message_end")]


def python_command(*arguments):
    if os.name == "nt":
        quote = lambda value: "'" + value.replace("'", "''") + "'"
        return "& " + " ".join(quote(value) for value in (sys.executable, *arguments))
    return " ".join(shlex.quote(value) for value in (sys.executable, *arguments))


def build_app(directory, config, responses, *, profiles=()):
    storage = PermissionStorage()
    session = SessionController(config, cwd=directory, permission_storage=storage)
    execution_config = ExecutionConfig(command_profiles=list(profiles)) if profiles else ExecutionConfig()
    runtime = ExecutionRuntime(directory, execution_config)
    registry = create_default_tool_registry()
    executor = ToolExecutor(registry, cwd=directory, execution_runtime=runtime,
                            permission_engine=PermissionEngine(storage))
    runner = TurnRunner(ScriptedProvider(responses), session, registry, executor)
    app = LanCherTextualApp(runner, config, session, UIConfig(), tool_registry=registry)
    return app, session, runner, storage


async def until(pilot, predicate, *, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await pilot.pause(0.02)


async def send(app, pilot, text, *, slash=False):
    composer = app.query_one(ComposerTextArea)
    composer.text = text
    composer.cursor_location = composer.document.end
    composer.focus()
    await pilot.pause()
    await pilot.press(*("tab", "enter") if slash else ("enter",))


async def finish(app, pilot):
    await until(pilot, lambda: not app._is_streaming and not app._turn_runner.has_active_turn)


async def wait_http_ready(client, url, pilot):
    # yield_ms=0 只结束启动调用的等待；默认配置没有就绪探针。
    async with asyncio.timeout(10):
        while True:
            try:
                return await client.get(url)
            except httpx.ConnectError:
                await pilot.pause(0.02)


async def approve_command(app, pilot):
    await until(pilot, lambda: bool(app.query(InlinePermissionPanel)))
    panel = app.query_one(InlinePermissionPanel)
    assert panel.request.tool_name == "run_command"
    assert panel.request.permission_policy == "default"
    request_id = panel.request.request_id
    panel.focus()
    await pilot.press("enter")
    await finish(app, pilot)
    return request_id


def last_result(session):
    return next(entry for entry in reversed(session.state.messages[-1].trace.entries) if entry.kind == "tool_result")


def process_info(runner, result):
    # 列表按 UUID 排序；本轮进程身份以工具结果为准。
    return next(item for item in runner.list_processes()
                if item["process_id"] == result.metadata["process_id"])


async def run_real_app(app, auto_pilot):
    loop = asyncio.get_running_loop()
    factory = loop.get_task_factory()
    try:
        # run_async 内部启用 eager_task_factory；run_test 没有这个启动步骤。
        async with asyncio.timeout(45):
            await app.run_async(headless=True, size=(100, 40), auto_pilot=auto_pilot)
    finally:
        loop.set_task_factory(factory)


@pytest.mark.asyncio
async def test_run_async_default_approval_starts_commands_after_completed_turn(tmp_path, openai_provider_config):
    version = python_command("--version")
    app, session, runner, storage = build_app(tmp_path, openai_provider_config,
        [reply(), command_call(version), reply(), command_call(version), reply()])

    async def drive(pilot):
        assert asyncio.get_running_loop().get_task_factory() is asyncio.eager_task_factory
        await send(app, pilot, "先完成一轮普通回答")
        await finish(app, pilot)
        owners = []
        approvals = []
        for _ in range(2):
            await send(app, pilot, "查看 Python 版本")
            approvals.append(await approve_command(app, pilot))
            result = last_result(session)
            assert result.ok, result.text
            assert "Python " in result.metadata["content"]
            info = process_info(runner, result)
            assert info["exit_code"] == 0
            assert info["origin_turn_id"]
            owners.append(info["origin_turn_id"])
        assert owners[0] != owners[1]
        assert len(set(approvals)) == 2
        assert not storage.rules_for_scope("session")
        app.exit(0)

    await run_real_app(app, drive)


@pytest.mark.asyncio
@pytest.mark.parametrize("use_profiles", [False, True], ids=["default", "declared-resources"])
async def test_run_async_background_http_survives_turns_then_session_stop_allows_new_turn(
    tmp_path, openai_provider_config, use_profiles,
):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    (tmp_path / "index.html").write_text("runtime-http-ready", encoding="utf-8")
    server = python_command("-u", "-m", "http.server", str(port), "--bind", "127.0.0.1")
    version = python_command("--version")
    profile = CommandProfile("回归服务器", server,
        resources=(ResourceClaim("external", f"port:{port}"),),
        readiness=ReadinessProbe("tcp", "127.0.0.1", port, 10000))
    # 已知只读命令可与端口资源并行；资源声明不会授予执行权限。
    version_profile = CommandProfile("只读版本查询", version, resources=())
    background_turn = [command_call(version), reply()] if use_profiles else [reply()]
    app, session, runner, storage = build_app(tmp_path, openai_provider_config,
        [reply(), command_call(server, lifetime="session", yield_ms=0), reply(),
         *background_turn, command_call(version), reply()],
        profiles=[profile, version_profile] if use_profiles else ())

    async def drive(pilot):
        await send(app, pilot, "先完成普通回答")
        await finish(app, pilot)
        await send(app, pilot, "启动会话 HTTP 服务")
        approvals = [await approve_command(app, pilot)]
        result = last_result(session)
        assert result.ok, result.text
        process_id = result.metadata["process_id"]
        if use_profiles:
            await until(pilot, lambda: process_info(runner, result)["readiness"] == "ready")
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            url = f"http://127.0.0.1:{port}/"
            response = await wait_http_ready(client, url, pilot)
            assert response.status_code == 200 and response.text == "runtime-http-ready"
            await send(app, pilot, f"/tasks show {process_id}", slash=True)
            await until(pilot, lambda: isinstance(app.screen, TasksScreen))
            assert app.screen.session_id == session.session_id
            await pilot.press("escape")
            assert (await client.get(url)).status_code == 200
            if use_profiles:
                await send(app, pilot, "服务器继续运行时查看 Python 版本")
                approvals.append(await approve_command(app, pilot))
                assert last_result(session).ok
            else:
                # 未声明命令保守持有项目锁，但不妨碍普通对话完成。
                await send(app, pilot, "服务器继续运行时完成普通回答")
                await finish(app, pilot)
                assert session.state.messages[-1].status == "complete"
                assert not app.query(InlinePermissionPanel)
            assert (await client.get(url)).status_code == 200
        await send(app, pilot, "/session stop", slash=True)
        await until(pilot, lambda: all(item["status"] not in {"starting", "running", "stopping"}
                                      for item in runner.list_processes()))
        with pytest.raises(OSError):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
        await send(app, pilot, "停止会话资源后再运行版本查询")
        approvals.append(await approve_command(app, pilot))
        result = last_result(session)
        assert result.ok, result.text
        assert process_info(runner, result)["origin_turn_id"]
        assert len(set(approvals)) == (3 if use_profiles else 2)
        assert not storage.rules_for_scope("session")
        app.exit(0)

    await run_real_app(app, drive)
