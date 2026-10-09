from __future__ import annotations

import asyncio
import json

import pytest

from lancher_code.context_management import SUMMARY_HEADINGS
from lancher_code.errors import ProviderRequestError
from lancher_code.models import MessageUsage, StreamEvent, ToolCallChunk, ToolDefinition, ToolExecutionResult
from lancher_code.session import SessionController
from lancher_code.sessions import ProjectSessionRepository, SessionBusyError, SessionRepositoryError
from lancher_code.sessions.codec import SessionCodec
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.turn_runner import TurnRunner


def reply(text):
    return [StreamEvent(kind="message_start"), StreamEvent(kind="text_delta", text=text),
            StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=5, output_tokens=3))]


def call_probe():
    return [StreamEvent(kind="message_start"), StreamEvent(
        kind="tool_call_delta", tool_call_chunk=ToolCallChunk(
            call_index=0, provider_call_id="call-probe", name_delta="probe", arguments_delta="{}",
        ),
    ), StreamEvent(kind="message_end")]


class Provider:
    def __init__(self, responses, before_request=None):
        self.responses = list(responses)
        self.requests = []
        self.before_request = before_request

    async def stream_chat(self, request):
        self.requests.append(request)
        if self.before_request is not None:
            self.before_request(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        for event in response:
            yield event


class ProbeTool:
    def __init__(self):
        self.calls = 0

    @property
    def definition(self):
        return ToolDefinition(name="probe", description="记录执行次数", category="read",
                              params_model={"type": "object", "properties": {}})

    async def execute(self, arguments, context):
        self.calls += 1
        return ToolExecutionResult(call_id="", tool_name="probe", content="已执行", is_error=False)


def runner_for(provider, config, root, tool=None):
    controller = SessionController(config, cwd=root)
    registry = ToolRegistry()
    if tool is not None:
        registry.register(tool)
    return TurnRunner(provider, controller, registry, ToolExecutor(registry, cwd=root)), controller


async def collect(runner, text):
    async def consume():
        return [event async for event in runner.run_user_turn(text)]
    return await asyncio.wait_for(consume(), timeout=5)


def test_empty_conversation_has_no_identity_or_storage(openai_provider_config, tmp_path):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    controller.set_work_phase("discuss")
    controller.set_permission_policy("acceptEdits")
    assert controller.session_id is None
    assert controller.paths is None
    assert controller.list_sessions() == []
    with pytest.raises(ValueError, match="不能为空"):
        controller.create_user_message(" \n ")
    controller.close()
    assert not (tmp_path / ".lancher" / "sessions").exists()


@pytest.mark.asyncio
async def test_first_input_is_durable_before_model_and_survives_provider_failure(openai_provider_config, tmp_path):
    repository = ProjectSessionRepository(tmp_path)
    checked = []
    def observe(request):
        info = repository.list_sessions()[0]
        snapshot = SessionCodec.project(repository.read(info.session_id))
        assert snapshot["messages"][0]["content"] == "第一条请求"
        checked.append(info.session_id)
    provider = Provider([ProviderRequestError("模拟模型故障")], observe)
    runner, controller = runner_for(provider, openai_provider_config, tmp_path)
    try:
        events = await collect(runner, "第一条请求")
        assert checked == [controller.session_id]
        assert any(event.kind == "turn_failed" for event in events)
        session_id = controller.session_id
        assert any(event["type"] == "turn.failed" for event in repository.read(session_id))
    finally:
        controller.close()
    restored = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        restored.resume_session(session_id)
        assert restored.state.messages[0].content == "第一条请求"
        assert restored.state.messages[-1].status == "error"
    finally:
        restored.close()


def test_rename_changes_only_title_and_new_is_lazy_and_isolated(openai_provider_config, tmp_path):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        controller.create_user_message("第一个对话")
        first_id, first_paths = controller.session_id, controller.paths
        first_paths.plan.write_text("第一份计划", encoding="utf-8")
        controller.rename_session(first_id, "有 空格的新标题")
        assert controller.session_id == first_id
        assert controller.paths == first_paths
        assert controller.list_sessions()[0].title == "有 空格的新标题"
        controller.new_session()
        assert controller.session_id is None and controller.paths is None
        assert len(controller.list_sessions()) == 1
        controller.create_user_message("第二个对话")
        assert controller.session_id != first_id
        assert controller.paths.root != first_paths.root
        assert not controller.paths.plan.exists()
        assert first_paths.plan.read_text(encoding="utf-8") == "第一份计划"
        controller.resume_session(first_id)
        assert controller.session_id == first_id
        assert controller.paths.plan.read_text(encoding="utf-8") == "第一份计划"
    finally:
        controller.close()


def test_controller_restore_rejects_second_writer_and_keeps_current_conversation(openai_provider_config, tmp_path):
    first = SessionController(openai_provider_config, cwd=tmp_path)
    second = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        first.create_user_message("正在使用")
        second.create_user_message("另一段对话")
        second_id = second.session_id
        with pytest.raises(SessionBusyError):
            second.resume_session(first.session_id)
        assert second.session_id == second_id
        assert second.state.messages[0].content == "另一段对话"
        first.close()
        second.resume_session(first.session_id)
        assert second.state.messages[0].content == "正在使用"
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_real_context_compaction_appends_boundary_and_preserves_original_log(openai_provider_config, tmp_path):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    repository = ProjectSessionRepository(tmp_path)
    summary = "<summary>" + "\n".join(f"## {heading}\n历史已归纳" for heading in SUMMARY_HEADINGS) + "</summary>"
    provider = Provider([reply(summary)])
    try:
        for index in range(4):
            controller.create_user_message(f"原始用户消息 {index}")
            message = controller.create_assistant_message()
            controller.append_message_content(message.id, f"原始助手回复 {index}")
            controller.complete_message(message.id)
        original = controller.paths.events.read_bytes()
        session_id = controller.session_id
        await controller.compact_context(provider=provider, visible_tools=[], persist=True)
        assert len(provider.requests) == 1
        assert controller.paths.events.read_bytes().startswith(original)
        records = repository.read(session_id)
        assert sum(event["type"] == "context.compacted" for event in records) == 1
        raw_users = [event["data"]["content"] for event in records
                     if event["type"] == "message.created" and event["data"]["role"] == "user"]
        assert raw_users == [f"原始用户消息 {index}" for index in range(4)]
        assert len(controller.state.messages) == 8
        assert "历史已归纳" in str(SessionCodec.project(records)["transcript"])
    finally:
        controller.close()


@pytest.mark.asyncio
async def test_creation_storage_failure_stops_before_model_and_stream_ends(openai_provider_config, tmp_path, monkeypatch):
    provider = Provider([reply("不应调用")])
    runner, controller = runner_for(provider, openai_provider_config, tmp_path)
    def fail(*args, **kwargs):
        raise SessionRepositoryError("磁盘不可写")
    monkeypatch.setattr(controller._sessions.repository, "create", fail)
    events = await collect(runner, "请修改项目")
    assert any(event.kind == "turn_failed" and "磁盘不可写" in event.error_text for event in events)
    assert not provider.requests
    assert not runner.has_active_turn
    assert controller.session_id is None
    controller.close()


@pytest.mark.asyncio
async def test_tool_start_storage_failure_prevents_tool_and_does_not_hang(openai_provider_config, tmp_path, monkeypatch):
    provider = Provider([call_probe(), reply("工具之后")])
    tool = ProbeTool()
    runner, controller = runner_for(provider, openai_provider_config, tmp_path, tool)
    controller.create_user_message("先建立对话")
    writer = controller._sessions.writer
    original = writer.append
    def append(kind, data, turn_id=None):
        if kind == "tool.started":
            raise SessionRepositoryError("工具边界无法落盘")
        return original(kind, data, turn_id)
    monkeypatch.setattr(writer, "append", append)
    try:
        events = await collect(runner, "运行工具")
        assert tool.calls == 0
        assert len(provider.requests) == 1
        assert any(event.kind == "turn_failed" for event in events)
        assert not runner.has_active_turn
    finally:
        controller.close()


def test_damaged_checkpoint_does_not_affect_recovery_from_events(openai_provider_config, tmp_path):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    controller.create_user_message("检查点丢失也保留此消息")
    session_id, paths = controller.session_id, controller.paths
    controller.close()
    paths.checkpoint.write_text("{corrupt", encoding="utf-8")
    restored = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        restored.resume_session(session_id)
        assert restored.state.messages[0].content == "检查点丢失也保留此消息"
    finally:
        restored.close()


def test_valid_older_checkpoint_replays_later_message_events(openai_provider_config, tmp_path):
    original = SessionController(openai_provider_config, cwd=tmp_path)
    original.create_user_message("检查点之前的消息")
    session_id, paths = original.session_id, original.paths
    original.close()
    older_checkpoint = paths.checkpoint.read_bytes()
    second = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        second.resume_session(session_id)
        second.create_user_message("检查点之后的消息")
    finally:
        second.close()
    paths.checkpoint.write_bytes(older_checkpoint)
    restored = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        restored.resume_session(session_id)
        assert [message.content for message in restored.state.messages] == [
            "检查点之前的消息", "检查点之后的消息",
        ]
    finally:
        restored.close()


def test_corrupt_target_projection_releases_lock_and_keeps_source(openai_provider_config, tmp_path):
    target = SessionController(openai_provider_config, cwd=tmp_path)
    target.create_user_message("目标对话")
    assistant = target.create_assistant_message()
    target_id = target.session_id
    target.close()
    repository = ProjectSessionRepository(tmp_path)
    with repository.open(target_id) as writer:
        writer.append("message.updated", {"id": assistant.id, "fields": {
            "status": "streaming", "trace": {"entries": [{"kind": "tool_call", "metadata": None}]},
        }})
    source = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        source.create_user_message("当前对话必须保留")
        source_id = source.session_id
        with pytest.raises(SessionRepositoryError):
            source.resume_session(target_id)
        assert source.session_id == source_id
        assert source.state.messages[0].content == "当前对话必须保留"
        with repository.open(target_id):
            pass
    finally:
        source.close()
