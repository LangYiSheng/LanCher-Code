"""真实 run_async + 原生后台进程 + 离线模型协议，验证退出小结的整条链路。"""
from __future__ import annotations

from conftest import app_config_for

import asyncio
import io
import os
import shlex
import sys
from uuid import uuid4

import httpx
from rich.console import Console

import lancher_code.app as app_module
from lancher_code.execution.contracts import ProcessSpec
from lancher_code.config.bootstrap import ConfigBootstrapState
from lancher_code.config.models import AppConfig, RuntimeConfig
from lancher_code.providers.factory import create_provider
from lancher_code.tui.app import ChatTUI
from lancher_code.tui.composer import ComposerTextArea


async def test_exit_after_real_session_and_background_process_prints_final_usage(
    monkeypatch, tmp_path, openai_provider_config, ui_config,
):
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=100)
    config = app_config_for(provider=openai_provider_config, ui=ui_config, runtime=RuntimeConfig())
    state = ConfigBootstrapState(config_path=tmp_path / "config.yaml", needs_setup=False)
    monkeypatch.setattr(app_module, "Console", lambda: console)
    monkeypatch.setattr(app_module, "resolve_config_bootstrap_state", lambda: state)
    monkeypatch.setattr(app_module, "load_config", lambda path: config)
    monkeypatch.setattr(app_module, "load_mcp_config", lambda cwd: ([], []))
    monkeypatch.setattr(app_module, "get_global_permissions_path", lambda: tmp_path / "global-permissions.json")
    monkeypatch.setattr(app_module, "get_global_mcp_config_path", lambda: tmp_path / "global-mcp.json")
    body = (
        b'data: {"choices":[{"index":0,"delta":{"content":"hello"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,'
        b'"prompt_tokens_details":{"cached_tokens":6}}}\n\n'
        b'data: [DONE]\n\n'
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    monkeypatch.setattr(app_module, "create_provider", lambda config, **kwargs: create_provider(
        config, client_factory=lambda: httpx.AsyncClient(transport=transport), **kwargs))
    captured = {}

    class HeadlessTUI(ChatTUI):
        async def run(self):
            app = self._app
            runner = app._turn_runner

            async def drive(pilot):
                composer = app.query_one(ComposerTextArea)
                composer.text = "退出链路测试"
                await pilot.press("enter")
                async with asyncio.timeout(10):
                    while app._is_streaming or not app._session_controller.session_id:
                        await pilot.pause(0.02)
                session_id = app._session_controller.session_id
                script = 'import time; print("ready", flush=True); time.sleep(30)'
                argv = (sys.executable, "-c", script)
                command = ("& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
                           if os.name == "nt" else shlex.join(argv))
                info = await runner._execution_runtime.processes.start(
                    ProcessSpec(command, "退出后台回归", tmp_path, yield_ms=100, lifetime="session", max_runtime_ms=30000),
                    session_id=session_id, turn_id=uuid4().hex, invocation_id=uuid4().hex,
                )
                assert runner.application_process_count == 1
                captured.update(session_id=session_id, process_id=info.process_id, runner=runner)
                assert "先下班" not in output.getvalue()
                await pilot.press("ctrl+c")
                assert app._exit_flow.is_armed and runner.application_process_count == 1
                assert "1 个托管进程" in app._exit_confirmation_text()
                await pilot.press("ctrl+c")

            result = await app.run_async(headless=True, auto_pilot=drive, size=(60, 20))
            return 0 if result is None else result

    monkeypatch.setattr(app_module, "ChatTUI", HeadlessTUI)
    assert await app_module.run_app() == 0
    text = output.getvalue()
    assert text.count("先下班") == 1
    assert f"/session resume {captured['session_id']}" in text
    assert "输入：10 tokens" in text and "输出：2 tokens" in text
    assert "缓存命中：6 tokens" in text and "缓存比：60.0%" in text
    assert "总计：12 tokens" in text and "已收尾 1 个托管进程" in text
    runner = captured["runner"]
    assert runner.application_process_count == 0
    assert runner._execution_runtime.processes.get(captured["process_id"], captured["session_id"]).status == "cancelled"
    from lancher_code.sessions.repository import ProjectSessionRepository
    with ProjectSessionRepository(tmp_path).open(captured["session_id"]):
        pass  # 成功重新取得写入锁，退出收尾确实已释放 Session。
