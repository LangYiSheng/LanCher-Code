from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
import httpx

from lancher_code.errors import ConfigError, ProviderRequestError
from lancher_code.logging_system import sanitize_log_text
from lancher_code.providers.catalog import resolve_model
from lancher_code.config.models import AppConfig
from lancher_code.contracts.messages import ChatRequest, ContentBlock, StreamEvent
from lancher_code.context.models import ContextUsageAnchor
from lancher_code.providers.models import ModelDefinition, ProviderConfig, ProviderDefinition, ThinkingConfig
from lancher_code.contracts.tools import ToolCall, ToolDefinition, ToolExecutionResult
from lancher_code.providers.claude import ClaudeProvider
from lancher_code.providers.openai import OpenAIProvider
from lancher_code.providers.factory import create_provider
from lancher_code.sessions.controller import SessionController
from lancher_code.sessions.storage import SessionRepositoryError
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.agent.runner import TurnRunner


def _catalog() -> AppConfig:
    return AppConfig(
        providers={
            "deepseek": ProviderDefinition(
                name="DeepSeek",
                protocol="openai",
                base_url="https://deepseek.example/v1",
                api_key="deepseek-secret-key",
                models={
                    "chat": ModelDefinition(model_name="deepseek-chat", context_window=128000),
                    "reasoner": ModelDefinition(model_name="deepseek-reasoner", display_name="推理"),
                },
            ),
            "anthropic": ProviderDefinition(
                name="Anthropic",
                protocol="claude",
                base_url="https://anthropic.example/v1",
                api_key="anthropic-secret-key",
                models={
                    "sonnet": ModelDefinition(
                        model_name="claude-sonnet",
                        context_window=200000,
                        thinking=ThinkingConfig(enabled=True, budget_tokens=1024),
                    )
                },
            ),
        },
        default_model="deepseek/chat",
    )


class RecordingProvider:
    def __init__(self, config: ProviderConfig, text: str = "模型回复") -> None:
        self.config = config
        self.text = text
        self.requests: list[ChatRequest] = []
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def stream_chat(self, request: ChatRequest):
        self.requests.append(request)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        yield StreamEvent(kind="text_delta", text=self.text)
        yield StreamEvent(kind="message_end", assistant_blocks=[ContentBlock.text_block(self.text)], response_complete=True)


async def _wait_provider_started(provider, task):
    started = asyncio.create_task(provider.started.wait())
    try:
        done, _ = await asyncio.wait((started, task), timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        assert started in done, "模型请求尚未开始，后台任务可能已失败。"
    finally:
        started.cancel()


def _runner(tmp_path: Path):
    config = _catalog()
    initial = RecordingProvider(resolve_model(config.providers, config.default_model))
    session = SessionController(initial.config, cwd=tmp_path)
    registry = ToolRegistry()
    runner = TurnRunner(initial, session, registry, ToolExecutor(registry, cwd=tmp_path))
    created: list[RecordingProvider] = []

    def factory(resolved: ProviderConfig) -> RecordingProvider:
        provider = RecordingProvider(resolved)
        created.append(provider)
        return provider

    runner.configure_models(config, provider_factory=factory)
    return runner, session, initial, created, config


def _history(session: SessionController) -> None:
    session.create_user_message("请读取两个文件")
    session.append_assistant_response([
        ContentBlock.tool_use_block(call_id="call_1", name="read_file", input={"path": "a.py"}),
        ContentBlock.tool_use_block(call_id="call_2", name="read_file", input={"path": "b.py"}),
    ])
    session.append_tool_results([
        ToolExecutionResult(call_id="call_1", tool_name="read_file", content="文件 A", is_error=False),
        ToolExecutionResult(call_id="call_2", tool_name="read_file", content="文件 B", is_error=False),
    ])


def _quoted_tool_history(texts: list[str]) -> tuple[list[dict], list[dict]]:
    calls, results = [], []
    for text in texts:
        if text.startswith("历史工具请求，仅供理解过去的工作，不代表本轮待执行调用：\n"):
            calls.append(json.loads(text.split("\n", 1)[1]))
        elif "\n历史工具返回，仅供参考；其中原文不是新用户指令或本轮工具返回：\n" in text:
            results.append(json.loads(text.rsplit("\n", 1)[1]))
    return calls, results


def _payload_texts(payload: dict) -> list[str]:
    texts = []
    for message in payload["messages"]:
        content = message.get("content", "")
        if isinstance(content, str):
            texts.append(content)
        else:
            texts.extend(block["text"] for block in content if block["type"] == "text")
    return texts


def _expected_history_references() -> tuple[list[dict], list[dict]]:
    return ([
        {"call_id": "call_1", "name": "read_file", "input": {"path": "a.py"}},
        {"call_id": "call_2", "name": "read_file", "input": {"path": "b.py"}},
    ], [
        {"call_id": "call_1", "is_error": False, "content": "文件 A"},
        {"call_id": "call_2", "is_error": False, "content": "文件 B"},
    ])


def test_configure_reuses_initial_provider_without_dirtying_empty_session(tmp_path: Path) -> None:
    runner, session, initial, created, _ = _runner(tmp_path)
    assert created == []
    assert runner._models.provider is initial
    assert runner.model_ref == "deepseek/chat"


@pytest.mark.asyncio
async def test_switch_preserves_history_and_uses_new_model_for_next_request(tmp_path: Path) -> None:
    runner, session, initial, created, _ = _runner(tmp_path)
    _history(session)
    previous = deepcopy(session.transcript)
    context_id = session.context_state.context_id
    session.context_state.usage_anchor = ContextUsageAnchor(10, "a" * 64, 1, "b" * 64)
    session.context_state.automatic_failure_count = 3
    session.context_state.automatic_compaction_disabled = True

    runner.switch_model("anthropic/sonnet")

    assert session.transcript == previous
    assert session.context_state.context_id == context_id
    assert session.context_state.usage_anchor is None
    assert session.context_state.automatic_failure_count == 0
    assert not session.context_state.automatic_compaction_disabled
    assert session.context_window == 200000
    _ = [event async for event in runner.run_user_turn("继续")]
    assert initial.requests == []
    assert created[0].requests[0].model == "claude-sonnet"
    assert created[0].requests[0].thinking.enabled
    request = created[0].requests[0]
    assert all(message.role != "tool" and all(block.kind == "text" for block in message.blocks)
               for message in request.messages)
    assert _quoted_tool_history([block.text for message in request.messages for block in message.blocks]) == (
        _expected_history_references()
    )
    assert session.transcript[:len(previous)] == previous
    assert created[0].requests[0].messages[0].blocks[-1].text == "请读取两个文件"
    assert "anthropic-secret-key" not in sanitize_log_text("anthropic-secret-key")


@pytest.mark.parametrize("target", ["deepseek/chat", "deepseek/reasoner", "anthropic/sonnet"])
def test_switch_keeps_all_parallel_tool_history_in_both_protocols(tmp_path: Path, target: str) -> None:
    runner, session, _, _, _ = _runner(tmp_path)
    _history(session)
    previous = deepcopy(session.transcript)
    runner.switch_model(target)
    assert session.transcript == previous
    request = session.build_request([], allow_tool_calls=True)
    openai_payload = OpenAIProvider(session.provider_config)._build_payload(request)
    results = [message for message in openai_payload["messages"] if message["role"] == "tool"]
    claude_payload = ClaudeProvider(session.provider_config)._build_payload(request)
    claude_results = [block for message in claude_payload["messages"] for block in message["content"]
                      if block["type"] == "tool_result"]
    if target != "deepseek/chat":
        assert results == claude_results == []
        assert all(message.role != "tool" and all(block.kind == "text" for block in message.blocks)
                   for message in request.messages)
        assert all("tool_calls" not in message for message in openai_payload["messages"])
        assert _quoted_tool_history(_payload_texts(openai_payload)) == _expected_history_references()
        assert _quoted_tool_history(_payload_texts(claude_payload)) == _expected_history_references()
    else:
        # 来源模型未变时保留当前协议中的并行工具交换。
        assert request.messages[1:len(previous)] == previous[1:]
        assert [(message["tool_call_id"], message["content"]) for message in results] == [
            ("call_1", "文件 A"), ("call_2", "文件 B")
        ]
        assert [block["tool_use_id"] for block in claude_results] == ["call_1", "call_2"]
    assert session.transcript[:len(previous)] == previous
    assert session.transcript[-1].blocks[0].text.startswith('<host_update ')


def test_failed_selection_or_factory_keeps_previous_runtime(tmp_path: Path) -> None:
    runner, session, initial, _, _ = _runner(tmp_path)
    session.create_user_message("保留这条历史")
    previous = deepcopy(session.transcript)
    with pytest.raises(ConfigError):
        runner.switch_model("missing/model")

    def failing_factory(config):
        raise RuntimeError("创建失败")

    runner._models._provider_factory = failing_factory
    with pytest.raises(RuntimeError, match="创建失败"):
        runner.switch_model("anthropic/sonnet")
    assert runner._models.provider is initial
    assert runner.model_ref == "deepseek/chat"
    assert session.provider_config.model == "deepseek-chat"
    assert session.transcript == previous


@pytest.mark.asyncio
async def test_switch_is_rejected_during_active_turn(tmp_path: Path) -> None:
    runner, session, initial, created, _ = _runner(tmp_path)
    initial.release = asyncio.Event()

    async def consume():
        return [event async for event in runner.run_user_turn("等待")]

    task = asyncio.create_task(consume())
    await _wait_provider_started(initial, task)
    try:
        with pytest.raises(ConfigError, match="等待完成"):
            runner.switch_model("anthropic/sonnet")
        assert created == []
    finally:
        initial.release.set()
        await task
    assert runner.model_ref == "deepseek/chat"


@pytest.mark.asyncio
async def test_manual_compaction_uses_new_model_and_prevents_switch(tmp_path: Path) -> None:
    runner, session, _, created, _ = _runner(tmp_path)
    _history(session)
    # 已结束的长旧轮次可交给摘要，最新短请求仍须保留。
    completed = session.create_assistant_message()
    session.append_message_content(completed.id, "旧轮次的文件分析与修复已经完成。\n" * 1_500)
    session.complete_message(completed.id)
    session.create_user_message("继续检查下一项。")
    runner.switch_model("anthropic/sonnet")
    provider = created[0]
    headings = ("主要请求和意图", "关键技术概念", "文件和代码段", "错误与修复", "问题解决过程", "用户消息与明确反馈", "待办任务", "当前工作", "可能的下一步")
    provider.text = "<summary>" + "\n".join(f"## {heading}\n内容" for heading in headings) + "</summary>"
    provider.release = asyncio.Event()
    task = asyncio.create_task(runner.compact_context())
    await _wait_provider_started(provider, task)
    try:
        with pytest.raises(ConfigError, match="压缩上下文"):
            runner.switch_model("deepseek/chat")
    finally:
        provider.release.set()
        result = await task
    assert provider.requests[0].model == "claude-sonnet"
    assert not runner._manual_compaction
    assert result.after_tokens < result.before_tokens
    assert session.transcript[-1].blocks[-1].text == "继续检查下一项。"


def test_reload_keeps_active_selection_when_default_changes(tmp_path: Path) -> None:
    runner, session, initial, created, config = _runner(tmp_path)
    updated = deepcopy(config)
    updated.default_model = "anthropic/sonnet"
    assert not runner.reload_models(updated)
    assert runner.model_ref == "deepseek/chat"
    assert runner.model_config.default_model == "anthropic/sonnet"
    assert runner._models.provider is initial
    assert created == []

    updated.providers["deepseek"].models["chat"].api_key = "changed-model-key"
    updated.providers["deepseek"].models["chat"].base_url = "https://override.example/v1"
    assert not runner.reload_models(updated)
    assert len(created) == 1
    assert session.provider_config.api_key == "changed-model-key"
    assert session.provider_config.base_url == "https://override.example/v1"


def test_reload_removed_model_uses_default_and_failed_reload_is_atomic(tmp_path: Path) -> None:
    runner, session, initial, _, config = _runner(tmp_path)
    invalid = deepcopy(config)
    invalid.providers["deepseek"].models["chat"].api_key = ""
    with pytest.raises(ConfigError):
        runner.reload_models(invalid)
    assert runner._models.provider is initial
    assert runner.model_config.providers["deepseek"].models["chat"].api_key is None

    updated = deepcopy(config)
    del updated.providers["deepseek"]
    updated.default_model = "anthropic/sonnet"
    assert runner.reload_models(updated)
    assert runner.model_ref == "anthropic/sonnet"
    assert "已被删除" in runner.model_notice


def test_model_reload_does_not_apply_pending_system_settings(tmp_path: Path) -> None:
    runner, session, _, _, config = _runner(tmp_path)
    updated = deepcopy(config)
    updated.runtime.experimental_mcp_tool_append = True
    assert not runner.reload_models(updated)
    assert not runner.model_config.runtime.experimental_mcp_tool_append
    assert not session.build_request([], allow_tool_calls=True).experimental_mcp_tool_append
    runner.apply_runtime_settings(updated.runtime)
    assert runner.model_config.runtime.experimental_mcp_tool_append
    assert session.build_request([], allow_tool_calls=True).experimental_mcp_tool_append


def test_switch_autosaves_reference_without_credentials(tmp_path: Path) -> None:
    runner, session, _, _, _ = _runner(tmp_path)
    session.create_user_message("已保存的历史")
    runner.switch_model("anthropic/sonnet")
    raw = session.paths.events.read_text(encoding="utf-8")
    assert json.loads(raw.splitlines()[0])["version"] == 2
    assert session.list_sessions().items[0].model_ref == "anthropic/sonnet"
    assert "secret-key" not in raw
    assert "base_url" not in raw


def test_switch_rolls_back_when_session_autosave_fails(tmp_path: Path, monkeypatch) -> None:
    runner, session, initial, _, _ = _runner(tmp_path)
    session.create_user_message("持久化会话")
    session.context_state.usage_anchor = ContextUsageAnchor(10, "a" * 64, 0, "b" * 64)
    session.flush()

    def fail_save(*args):
        raise SessionRepositoryError("磁盘不可写")

    monkeypatch.setattr(session._sessions.writer, "append", fail_save)
    with pytest.raises(SessionRepositoryError, match="磁盘不可写"):
        runner.switch_model("anthropic/sonnet")
    assert runner._models.provider is initial
    assert runner.model_ref == "deepseek/chat"
    assert session.context_state.usage_anchor is not None


@pytest.mark.parametrize("saved_ref", ["anthropic/sonnet", "removed/model", None])
def test_resume_resolves_saved_model_or_default_and_discards_usage_anchor(tmp_path: Path, saved_ref: str | None) -> None:
    runner, session, _, _, _ = _runner(tmp_path)
    original = SessionController(resolve_model(runner.model_config.providers, runner.model_config.default_model), cwd=tmp_path, selected_model_ref=saved_ref)
    original.create_user_message("历史消息")
    original.context_state.usage_anchor = ContextUsageAnchor(99, "a" * 64, 1, "b" * 64)
    saved_id = original.session_id
    original.close()
    runner.switch_model("deepseek/reasoner")

    assert runner.resume_session(saved_id) == 0

    expected = "anthropic/sonnet" if saved_ref == "anthropic/sonnet" else "deepseek/chat"
    assert runner.model_ref == expected
    assert session.context_state.usage_anchor is None
    assert session.state.messages[0].content == "历史消息"
    assert bool(runner.model_notice) == (saved_ref != "anthropic/sonnet")


def test_failed_resume_preparation_keeps_current_session(tmp_path: Path) -> None:
    runner, session, initial, _, _ = _runner(tmp_path)
    original = SessionController(resolve_model(runner.model_config.providers, runner.model_config.default_model), cwd=tmp_path, selected_model_ref="anthropic/sonnet")
    original.create_user_message("另一个会话")
    saved_id = original.session_id
    original.close()
    session.create_user_message("当前历史")

    def failing_factory(config):
        raise RuntimeError("创建失败")

    runner._models._provider_factory = failing_factory
    with pytest.raises(RuntimeError, match="创建失败"):
        runner.resume_session(saved_id)
    assert runner._models.provider is initial
    assert runner.model_ref == "deepseek/chat"
    assert session.state.messages[0].content == "当前历史"
    assert session.session_id != saved_id


@pytest.mark.asyncio
async def test_http_switch_uses_inherited_and_overridden_connection_and_keeps_tool_results(tmp_path: Path) -> None:
    config = _catalog()
    override = config.providers["deepseek"].models["reasoner"]
    override.protocol = "claude"
    override.base_url = "https://model-override.example/custom"
    override.api_key = "model-override-secret"
    override.model_name = "custom-anthropic-model"
    expected = [
        ("deepseek/chat", "https://deepseek.example/v1/chat/completions", "openai", "deepseek-secret-key", "deepseek-chat"),
        ("deepseek/reasoner", "https://model-override.example/custom/messages", "claude", "model-override-secret", "custom-anthropic-model"),
        ("deepseek/chat", "https://deepseek.example/v1/chat/completions", "openai", "deepseek-secret-key", "deepseek-chat"),
        ("anthropic/sonnet", "https://anthropic.example/v1/messages", "claude", "anthropic-secret-key", "claude-sonnet"),
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _, url, protocol, api_key, model_name = expected[len(seen)]
        seen.append(request)
        assert str(request.url) == url
        payload = json.loads(request.content)
        assert payload["model"] == model_name
        if protocol == "openai":
            assert request.headers["authorization"] == f"Bearer {api_key}"
            assert "x-api-key" not in request.headers
            results = [message for message in payload["messages"] if message["role"] == "tool"]
            assert [message["tool_call_id"] for message in results] == ["call_1", "call_2"]
            content = 'data: {"choices":[{"delta":{"content":"回答"}}]}\n\ndata: [DONE]\n\n'
        else:
            assert request.headers["x-api-key"] == api_key
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert "authorization" not in request.headers
            results = [block for message in payload["messages"] for block in message["content"] if block["type"] == "tool_result"]
            assert results == []
            assert all(block["type"] == "text" for message in payload["messages"] for block in message["content"])
            assert _quoted_tool_history(_payload_texts(payload)) == _expected_history_references()
            content = 'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"回答"}}\n\ndata: {"type":"message_stop"}\n\n'
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content.encode())

    transport = httpx.MockTransport(handler)

    def factory(resolved: ProviderConfig):
        return create_provider(resolved, client_factory=lambda: httpx.AsyncClient(transport=transport))

    initial = factory(resolve_model(config.providers, config.default_model))
    session = SessionController(resolve_model(config.providers, config.default_model), cwd=tmp_path)
    registry = ToolRegistry()
    runner = TurnRunner(initial, session, registry, ToolExecutor(registry, cwd=tmp_path))
    runner.configure_models(config, provider_factory=factory)
    _history(session)
    original_history = deepcopy(session.transcript)
    for index, (model_ref, *_rest) in enumerate(expected):
        if index:
            before_switch = deepcopy(session.transcript)
            runner.switch_model(model_ref)
            assert session.transcript == before_switch
        events = [event async for event in runner.run_user_turn(f"继续第 {index + 1} 轮")]
        assert events[-1].kind == "turn_completed"
        assert session.transcript[:len(original_history)] == original_history
    assert len(seen) == len(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "runtime_error", "provider_error"])
async def test_interrupted_tools_are_paired_before_cross_protocol_switch(tmp_path: Path, interruption: str) -> None:
    config = _catalog()
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()
    executions: list[bool] = []
    requests: list[dict] = []

    class WaitingTool:
        @property
        def definition(self):
            return ToolDefinition(name="wait_tool", description="测试等待", input_schema={"type": "object"})

        async def execute(self, arguments, context):
            executions.append(arguments["wait"])
            if arguments["wait"]:
                tool_started.set()
                await release_tool.wait()
            return ToolExecutionResult(call_id="", tool_name="wait_tool", content="已完成的工具结果", is_error=False)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if request.url.path.endswith("/chat/completions"):
            index = len(requests)
            tool_call = {"index": 0, "id": f"call_{index}", "function": {"name": "wait_tool", "arguments": json.dumps({"wait": index == 2})}}
            event = {"choices": [{"delta": {"tool_calls": [tool_call]}}]}
            content = f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n"
        else:
            assert payload["model"] == "claude-sonnet"
            calls = [block["id"] for message in payload["messages"] for block in message["content"] if block["type"] == "tool_use"]
            results = [block for message in payload["messages"] for block in message["content"] if block["type"] == "tool_result"]
            assert calls == results == []
            assert all(block["type"] == "text" for message in payload["messages"] for block in message["content"])
            quoted_calls, quoted_results = _quoted_tool_history(_payload_texts(payload))
            assert quoted_calls == [
                {"call_id": "call_1", "name": "wait_tool", "input": {"wait": False}},
                {"call_id": "call_2", "name": "wait_tool", "input": {"wait": True}},
            ]
            assert [result["call_id"] for result in quoted_results] == ["call_1", "call_2"]
            assert quoted_results[0]["content"] == "已完成的工具结果"
            assert quoted_results[0]["is_error"] is False
            assert quoted_results[1]["is_error"] is True
            if interruption == "cancel":
                assert "未获得" in quoted_results[1]["content"]
                assert "可能已部分执行" in quoted_results[1]["content"]
            else:
                # 执行器在调用工具之前失败，明确未执行，仍须补齐协议结果。
                assert "尚未启动，没有执行" in quoted_results[1]["content"]
                assert "可能已部分执行" not in quoted_results[1]["content"]
            content = 'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"继续完成"}}\n\ndata: {"type":"message_stop"}\n\n'
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content.encode())

    transport = httpx.MockTransport(handler)

    def factory(resolved):
        return create_provider(resolved, client_factory=lambda: httpx.AsyncClient(transport=transport))

    session = SessionController(resolve_model(config.providers, config.default_model), cwd=tmp_path)
    registry = ToolRegistry()
    registry.register(WaitingTool())
    executor = ToolExecutor(registry, cwd=tmp_path)
    if interruption != "cancel":
        original_execute = executor.execute_calls

        async def fail_second_batch(calls, **kwargs):
            if calls[0].arguments["wait"]:
                error = RuntimeError if interruption == "runtime_error" else ProviderRequestError
                raise error("测试执行器中断")
            return await original_execute(calls, **kwargs)

        executor.execute_calls = fail_second_batch
    runner = TurnRunner(factory(resolve_model(config.providers, config.default_model)), session, registry, executor)
    runner.configure_models(config, provider_factory=factory)

    async def consume():
        return [event async for event in runner.run_user_turn("先运行两个工具")]

    task = asyncio.create_task(consume())
    if interruption == "cancel":
        await asyncio.wait_for(tool_started.wait(), timeout=5)
        assert runner.cancel_active_turn()
    events = await asyncio.wait_for(task, timeout=5)
    assert events[-1].kind == ("turn_cancelled" if interruption == "cancel" else "turn_failed")
    assert not runner.has_active_turn
    interrupted_history = deepcopy(session.transcript)
    stored_calls = [block for message in interrupted_history for block in message.blocks if block.kind == "tool_use"]
    stored_results = [block for message in interrupted_history for block in message.blocks if block.kind == "tool_result"]
    assert [block.call_id for block in stored_calls] == ["call_1", "call_2"]
    assert [block.call_id for block in stored_results] == ["call_1", "call_2"]
    assert [block.is_error for block in stored_results] == [False, True]
    runner.switch_model("anthropic/sonnet")
    assert session.transcript == interrupted_history
    continuation = [event async for event in runner.run_user_turn("检查状态后继续")]
    assert continuation[-1].kind == "turn_completed"
    # 模型切换重建主机事件，但真实助手响应与已配对工具结果不得改写。
    assert [message for message in session.transcript if message.role in {'assistant', 'tool'}][:4] == [
        message for message in interrupted_history if message.role in {'assistant', 'tool'}
    ]
    assert len(requests) == 3
    assert executions == ([False, True] if interruption == "cancel" else [False])
