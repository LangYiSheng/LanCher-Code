from __future__ import annotations

import argparse
import asyncio
import builtins
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx

from lancher_code.cli import build_arg_parser, main
from lancher_code.sdk import AgentResult, create_agent
from lancher_code import headless
from lancher_code.agent.skills import SkillsService
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.providers.factory import create_provider
from conftest import app_config_for


class FakeAgent:
    def __init__(self, *, status="completed", error_text=None, wait=False):
        self.session_id = "a" * 32
        self.last_result = AgentResult(status=status, session_id=self.session_id, text="测试回答", error_text=error_text)
        self.wait = wait
        self.actions = []
        self.permissions = []

    async def __aenter__(self):
        self.actions.append("start")
        return self

    async def __aexit__(self, *exc):
        self.actions.append("close")

    def resume_session(self, session_id):
        self.actions.append(("resume", session_id))

    def switch_model(self, model_ref):
        self.actions.append(("model", model_ref))

    async def run(self, prompt):
        self.actions.append(("run", prompt))
        if self.wait:
            await asyncio.Event().wait()
        return self.last_result

    async def run_user_turn(self, prompt):
        self.actions.append(("stream", prompt))
        yield SimpleNamespace(
            kind="permission_request_created", permission_request=SimpleNamespace(request_id="approval"),
            to_dict=lambda: {"kind": "permission_request_created", "session_id": self.session_id, "sequence": 1},
        )
        assert self.permissions, "审批事件必须得到响应，不能留给不存在的界面。"
        yield SimpleNamespace(
            kind="turn_completed", permission_request=None,
            to_dict=lambda: {"kind": "turn_completed", "session_id": self.session_id, "sequence": 2},
        )

    def resolve_permission_request(self, resolution):
        self.permissions.append(resolution)
        return True

    async def stop_and_wait(self):
        self.actions.append("stop")


def arguments(tmp_path, **overrides):
    values = dict(prompt="任务", config=tmp_path / "explicit.yaml", cwd=tmp_path,
                  model=None, session=None, format="text", timeout=None)
    values.update(overrides)
    return argparse.Namespace(**values)


def install_agent(monkeypatch, agent):
    captured = {}
    config = SimpleNamespace(providers={}, to_agent_config=lambda: "explicit-agent-config")
    monkeypatch.setattr(headless, "load_config", lambda path: captured.setdefault("config_path", path) and config)

    def factory(config, **kwargs):
        captured.update(config=config, **kwargs)
        return agent

    monkeypatch.setattr(headless, "create_agent", factory)
    return captured


@pytest.mark.asyncio
async def test_text_run_uses_explicit_config_and_closes_before_output(monkeypatch, tmp_path, capsys):
    agent = FakeAgent()
    captured = install_agent(monkeypatch, agent)
    code = await headless.run_headless(arguments(tmp_path, model="demo/chat", session="b" * 32))
    output = capsys.readouterr()
    assert code == 0
    assert output.out == "测试回答\n"
    assert output.err == ""
    assert captured == dict(config_path=tmp_path / "explicit.yaml", config="explicit-agent-config",
                            cwd=tmp_path, model_ref="demo/chat")
    assert agent.actions == ["start", ("resume", "b" * 32), ("model", "demo/chat"), ("run", "任务"), "close"]


@pytest.mark.asyncio
async def test_json_failure_keeps_stdout_machine_readable(monkeypatch, tmp_path, capsys):
    agent = FakeAgent(status="failed", error_text="模型请求失败")
    install_agent(monkeypatch, agent)
    assert await headless.run_headless(arguments(tmp_path, format="json")) == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["status"] == "failed"
    assert result["session_id"] == agent.session_id
    assert result["error_text"] == "模型请求失败"
    assert "模型请求失败" in output.err


@pytest.mark.asyncio
async def test_jsonl_stream_denies_approval_and_emits_final_result(monkeypatch, tmp_path, capsys):
    agent = FakeAgent()
    install_agent(monkeypatch, agent)
    assert await headless.run_headless(arguments(tmp_path, format="jsonl")) == 0
    output = capsys.readouterr()
    rows = [json.loads(line) for line in output.out.splitlines()]
    assert [row["kind"] for row in rows] == ["permission_request_created", "turn_completed", "result"]
    assert rows[-1]["result"]["text"] == "测试回答"
    assert [(item.request_id, item.outcome) for item in agent.permissions] == [("approval", "deny")]
    assert agent.actions[-1] == "close"
    assert output.err == ""


@pytest.mark.asyncio
async def test_timeout_waits_for_stop_and_close_before_reporting(monkeypatch, tmp_path, capsys):
    agent = FakeAgent(wait=True)
    install_agent(monkeypatch, agent)
    assert await headless.run_headless(arguments(tmp_path, format="json", timeout=0.01)) == 124
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "timeout"
    assert agent.actions[-2:] == ["stop", "close"]


@pytest.mark.asyncio
async def test_cancellation_closes_resources_and_returns_130(monkeypatch, tmp_path, capsys):
    agent = FakeAgent(wait=True)
    install_agent(monkeypatch, agent)
    task = asyncio.create_task(headless.run_headless(arguments(tmp_path, format="json")))
    while ("run", "任务") not in agent.actions:
        await asyncio.sleep(0)
    task.cancel()
    assert await task == 130
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"
    assert agent.actions[-2:] == ["stop", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["", "  ", "-"])
async def test_empty_input_does_not_load_config_or_start_agent(monkeypatch, tmp_path, capsys, prompt):
    monkeypatch.setattr(sys, "stdin", io.StringIO(" \n"))
    monkeypatch.setattr(headless, "load_config", lambda _: pytest.fail("空任务不应读取配置"))
    assert await headless.run_headless(arguments(tmp_path, prompt=prompt, format="json")) == 1
    assert json.loads(capsys.readouterr().out)["error_text"] == "任务内容不能为空。"


@pytest.mark.asyncio
async def test_stdin_prompt_is_delivered_without_shell_interpretation(monkeypatch, tmp_path, capsys):
    agent = FakeAgent()
    install_agent(monkeypatch, agent)
    text = "第一行\n$HOME 与 `内容` 保持原样\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    assert await headless.run_headless(arguments(tmp_path, prompt="-")) == 0
    assert ("run", text) in agent.actions


@pytest.mark.asyncio
async def test_utf8_stdin_and_output_ignore_initial_gbk_encoding(monkeypatch, tmp_path):
    agent = FakeAgent()
    install_agent(monkeypatch, agent)
    prompt = "中文任务\n保留多行与表情 🌱\n"
    input_stream = io.TextIOWrapper(io.BytesIO(prompt.encode("utf-8")), encoding="gbk")
    output_bytes = io.BytesIO()
    error_bytes = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", input_stream)
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(output_bytes, encoding="gbk"))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(error_bytes, encoding="gbk"))
    assert await headless.run_headless(arguments(tmp_path, prompt="-", format="json")) == 0
    assert ("run", prompt) in agent.actions
    assert input_stream.encoding == "utf-8"
    assert json.loads(output_bytes.getvalue().decode("utf-8"))["text"] == "测试回答"
    assert error_bytes.getvalue() == b""


@pytest.mark.parametrize("output_format", ["json", "jsonl"])
def test_subprocess_machine_errors_are_utf8_with_gbk_environment(tmp_path, output_format):
    # 使用临时日志与缺失配置，真实子进程不会读取个人模型配置或请求模型。
    code = (
        "import sys; from pathlib import Path; "
        "from lancher_code.cli import main; "
        "import lancher_code.logging_system as logs; "
        "original = logs.configure_logging; "
        "logs.configure_logging = lambda: original(log_path=Path(sys.argv[1])); "
        "raise SystemExit(main(sys.argv[2:]))"
    )
    environment = os.environ.copy()
    environment.update(PYTHONIOENCODING="gbk", PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.run(
        [sys.executable, "-B", "-c", code, str(tmp_path / "error.log"), "run", "中文任务",
         "--config", str(tmp_path / "missing.yaml"), "--format", output_format],
        cwd=Path(__file__).resolve().parents[2], env=environment, capture_output=True, timeout=15,
    )
    assert process.returncode == 1
    row = json.loads(process.stdout.decode("utf-8"))
    result = row["result"] if output_format == "jsonl" else row
    assert result["status"] == "failed"
    assert "配置文件不存在" in result["error_text"]
    assert "配置文件不存在" in process.stderr.decode("utf-8")


@pytest.mark.asyncio
async def test_config_error_is_json_and_never_bootstraps_tui(monkeypatch, tmp_path, capsys):
    def missing_config(path):
        raise ValueError("配置文件不存在")
    monkeypatch.setattr(headless, "load_config", missing_config)
    monkeypatch.setattr(headless, "create_agent", lambda *a, **k: pytest.fail("无配置不应创建 Agent"))
    assert await headless.run_headless(arguments(tmp_path, format="jsonl")) == 1
    row = json.loads(capsys.readouterr().out)
    assert row["kind"] == "result" and row["result"]["status"] == "failed"


@pytest.mark.asyncio
async def test_error_result_redacts_registered_credentials(monkeypatch, tmp_path, capsys):
    agent = FakeAgent(status="failed", error_text="认证失败 provider-private-credential")
    agent.last_result = AgentResult(status="failed", session_id=agent.session_id,
        text="认证失败 provider-private-credential", error_text="认证失败 provider-private-credential")
    install_agent(monkeypatch, agent)
    headless.register_sensitive_values(["provider-private-credential"])
    assert await headless.run_headless(arguments(tmp_path, format="json")) == 1
    output = capsys.readouterr()
    assert "provider-private-credential" not in output.out + output.err
    assert "[REDACTED]" in json.loads(output.out)["error_text"]


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "bad"])
def test_cli_rejects_invalid_timeout(timeout):
    with pytest.raises(SystemExit) as error:
        build_arg_parser().parse_args(["run", "任务", "--config", "model.yaml", "--timeout", timeout])
    assert error.value.code == 2


def test_cli_requires_explicit_config():
    with pytest.raises(SystemExit) as error:
        build_arg_parser().parse_args(["run", "任务"])
    assert error.value.code == 2


@pytest.mark.parametrize("argv", [["--help"], ["run", "--help"]])
def test_help_does_not_import_tui_or_configure_logging(monkeypatch, capsys, argv):
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.startswith(("lancher_code.app", "lancher_code.tui", "textual", "rich")):
            pytest.fail(f"帮助命令不应加载界面：{name}")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(SystemExit) as error:
        main(argv)
    assert error.value.code == 0
    assert "run" in capsys.readouterr().out


def test_cli_run_dispatches_without_importing_app(monkeypatch, tmp_path):
    from lancher_code import logging_system
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name == "lancher_code.app":
            pytest.fail("无界面运行不应导入 TUI 装配")
        return original_import(name, *args, **kwargs)
    async def fake_run(args):
        assert args.prompt == "任务"
        assert args.config == Path("model.yaml")
        return 124
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(headless, "run_headless", fake_run)
    monkeypatch.setattr(logging_system, "configure_logging", lambda: None)
    monkeypatch.setattr(logging_system, "close_logging", lambda: None)
    assert main(["run", "任务", "--config", "model.yaml"]) == 124


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["text", "json", "jsonl"])
@pytest.mark.parametrize("requests_approval", [False, True])
async def test_headless_sdk_protocol_and_permissions_end_to_end(
    monkeypatch, tmp_path, capsys, openai_provider_config, output_format, requests_approval,
):
    project = tmp_path / "project"
    project.mkdir()
    config = app_config_for(openai_provider_config)
    captured_requests = []
    captured_agents = []

    def handle(request):
        payload = json.loads(request.content)
        captured_requests.append(payload)
        if requests_approval and len(captured_requests) == 1:
            delta = {"tool_calls": [{"index": 0, "id": "outside-write", "function": {
                "name": "write_file", "arguments": json.dumps({"path": "../blocked.txt", "content": "禁止写入"}),
            }}]}
        else:
            if requests_approval:
                tool_results = [item for item in payload["messages"] if item["role"] == "tool"]
                assert tool_results[-1]["tool_call_id"] == "outside-write"
                assert "拒绝" in tool_results[-1]["content"]
            delta = {"content": "离线协议回答"}
        frames = [
            {"choices": [{"delta": delta}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
        ]
        content = "".join(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n" for frame in frames)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=(content + "data: [DONE]\n\n").encode())

    transport = httpx.MockTransport(handle)
    def factory(agent_config, **kwargs):
        agent = create_agent(agent_config, **kwargs, enable_mcp=False, permission_storage=PermissionStorage(),
            skills_service=SkillsService(project, user_root=tmp_path / "isolated-user"),
            provider_factory=lambda provider_config: create_provider(provider_config,
                client_factory=lambda: httpx.AsyncClient(transport=transport)))
        captured_agents.append(agent)
        return agent

    monkeypatch.setattr(headless, "load_config", lambda _: config)
    monkeypatch.setattr(headless, "create_agent", factory)
    assert await headless.run_headless(arguments(tmp_path, cwd=project, format=output_format, timeout=5)) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert len(captured_requests) == (2 if requests_approval else 1)
    assert not (tmp_path / "blocked.txt").exists()
    assert captured_agents[0].session_id
    assert not captured_agents[0].has_active_turn
    assert (project / ".lancher" / "sessions" / captured_agents[0].session_id / "checkpoint.json").exists()
    if output_format == "text":
        assert output.out == "离线协议回答\n"
    else:
        rows = [json.loads(line) for line in output.out.splitlines()]
        result = rows[-1]["result"] if output_format == "jsonl" else rows[0]
        assert result["status"] == "completed"
        assert result["text"] == "离线协议回答"
        assert result["run_usage"]["request_count"] == len(captured_requests)
        if output_format == "jsonl" and requests_approval:
            assert any(row["kind"] == "permission_request_resolved" and
                       row["permission_resolution"]["outcome"] == "deny" for row in rows[:-1])
            assert any(row["kind"] == "tool_result_received" and
                       row["tool_result"]["error_code"] == "permission_user_denied" for row in rows[:-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["cancel", "timeout"])
async def test_cancelled_headless_sdk_keeps_reported_usage_and_closes_protocol(
    monkeypatch, tmp_path, capsys, openai_provider_config, interrupt,
):
    class WaitingStream(httpx.AsyncByteStream):
        def __init__(self):
            self.waiting = asyncio.Event()
            self.closed = False

        async def __aiter__(self):
            yield b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":1}}\n\n'
            self.waiting.set()
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    stream = WaitingStream()
    transport = httpx.MockTransport(lambda request: httpx.Response(200,
        headers={"content-type": "text/event-stream"}, stream=stream))
    config = app_config_for(openai_provider_config)
    agents = []
    def factory(agent_config, **kwargs):
        agent = create_agent(agent_config, **kwargs, enable_mcp=False, permission_storage=PermissionStorage(),
            skills_service=SkillsService(tmp_path, user_root=tmp_path / "isolated-user"),
            provider_factory=lambda provider_config: create_provider(provider_config,
                client_factory=lambda: httpx.AsyncClient(transport=transport)))
        agents.append(agent)
        return agent
    monkeypatch.setattr(headless, "load_config", lambda _: config)
    monkeypatch.setattr(headless, "create_agent", factory)
    task = asyncio.create_task(headless.run_headless(arguments(tmp_path, format="json",
        timeout=2.5 if interrupt == "timeout" else 10)))
    await asyncio.wait_for(stream.waiting.wait(), 5)
    if interrupt == "cancel":
        task.cancel()
    assert await task == (130 if interrupt == "cancel" else 124)
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == ("cancelled" if interrupt == "cancel" else "timeout")
    assert result["run_usage"]["request_count"] == 1
    assert result["run_usage"]["usage"]["input_tokens"] == 7
    assert stream.closed and not agents[0].has_active_turn
