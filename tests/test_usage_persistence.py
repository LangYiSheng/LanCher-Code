from __future__ import annotations

import copy
import json

import pytest

from lancher_code.models import MessageUsage, SessionState
from lancher_code.run_usage import RequestUsageRecord, RunUsageTracker
from lancher_code.session import SessionController
from lancher_code.sessions.codec import SessionCodec
from lancher_code.sessions.repository import ProjectSessionRepository, SessionRepositoryError
from lancher_code.sessions.service import SessionService


class MemoryWriter:
    """只收集事实事件，让规模验证不依赖磁盘速度。"""

    def __init__(self, initial):
        self.events = [{"type": "session.created", "data": {"initial_data": copy.deepcopy(initial)}}]
        self.saved_checkpoint = None

    def append(self, kind, data, turn_id=None):
        self.events.append({"type": kind, "data": copy.deepcopy(data), "turn_id": turn_id})

    def checkpoint(self, data):
        self.saved_checkpoint = copy.deepcopy(data)


def _memory_service():
    snapshot = SessionCodec.encode(SessionState(), [], [], None)
    service = SessionService.__new__(SessionService)
    service._saved = copy.deepcopy(snapshot)
    service.writer = MemoryWriter(snapshot)
    return service, snapshot


def _record(index=1, *, status="completed", session_id=None, run_id="run", message_id=None):
    return RequestUsageRecord(
        request_id=f"{index:032x}", run_id=run_id, protocol="openai", model="gpt-test",
        session_id=session_id, turn_id=f"turn-{index}", message_id=message_id, status=status,
        usage=MessageUsage(input_tokens=17, output_tokens=3, cached_input_tokens=0,
                           is_final=status == "completed"),
    ).to_dict()


def test_one_hundred_request_updates_keep_event_payloads_linear_and_checkpoint_complete():
    service, snapshot = _memory_service()
    for index in range(1, 101):
        record = _record(index)
        snapshot["state"]["request_usage"][record["request_id"]] = record
        service.record_usage(record, turn_id=record["turn_id"])
        # 阶段与锚点会随着真实请求改变，不能因此反复复制已积累的账本。
        snapshot["state"]["work_phase"] = "plan" if index % 2 else "execute"
        snapshot["state"]["plan_mode_turn_count"] = index
        service.persist(snapshot)
        service.persist(snapshot)

    events = service.writer.events[1:]
    usage_events = [event for event in events if event["type"] == "usage.request_updated"]
    state_events = [event for event in events if event["type"] == "state.changed"]
    assert len(events) == 200
    assert len(usage_events) == len(state_events) == 100
    assert len({event["data"]["request_id"] for event in usage_events}) == 100
    assert all("request_usage" not in event["data"] for event in state_events)
    # 每一份请求记录在日志中只出现一次，不再随状态事件产生 1+2+...+100 次重复。
    assert sum(json.dumps(event["data"]).count('"request_id"') for event in events) == 100
    assert SessionCodec.project(service.writer.events) == snapshot
    service.checkpoint()
    assert service.writer.saved_checkpoint == snapshot
    assert len(service.writer.saved_checkpoint["state"]["request_usage"]) == 100


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "incomplete"])
def test_persist_records_terminal_changes_without_callback_or_duplicate_snapshots(status):
    service, snapshot = _memory_service()
    running = _record(status="running")
    service.record_usage(running, turn_id=running["turn_id"])
    terminal = copy.deepcopy(running)
    terminal["status"] = status
    terminal["usage"]["is_final"] = status == "completed"
    snapshot["state"]["request_usage"][terminal["request_id"]] = terminal
    service.persist(snapshot)
    service.persist(snapshot)
    service.record_usage(terminal, turn_id=terminal["turn_id"])

    updates = [event for event in service.writer.events if event["type"] == "usage.request_updated"]
    assert [event["data"]["status"] for event in updates] == ["running", status]
    assert updates[-1]["turn_id"] == terminal["turn_id"]
    assert updates[-1]["data"]["usage"]["input_tokens"] == 17
    assert updates[-1]["data"]["usage"]["output_tokens"] == 3
    assert SessionCodec.project(service.writer.events) == snapshot
    assert not any(event["type"] == "state.changed" for event in service.writer.events)


def test_persist_refuses_to_remove_saved_usage_history():
    service, snapshot = _memory_service()
    record = _record()
    service.record_usage(record)
    before = copy.deepcopy(service.writer.events)
    with pytest.raises(SessionRepositoryError, match="不能删除"):
        service.persist(snapshot)
    assert service.writer.events == before
    assert service._saved["state"]["request_usage"] == {record["request_id"]: record}


@pytest.mark.parametrize("kind", ["state.changed", "usage.request_updated"])
def test_incremental_events_cannot_invent_missing_initial_usage_ledger(kind):
    initial = SessionCodec.encode(SessionState(), [], [], None)
    assert initial["state"]["context_management"]["version"] == 2
    missing_ledger = copy.deepcopy(initial)
    del missing_ledger["state"]["request_usage"]
    data = initial["state"] if kind == "state.changed" else _record()
    events = [{"type": "session.created", "data": {"initial_data": missing_ledger}},
              {"type": kind, "data": data}]
    with pytest.raises(SessionRepositoryError, match="缺少请求用量账本"):
        SessionCodec.project(events)


def _report(controller, index, *, status="completed", message_id=None):
    request = controller.bind_usage_request(controller.build_request([], allow_tool_calls=False),
                                            turn_id=f"turn-{index}", message_id=message_id)
    record = _record(index, status=status, session_id=controller.session_id,
                     run_id=request.run_id, message_id=message_id)
    record["request_id"] = request.request_id
    request.usage_callback(record)
    return record


def test_disk_checkpoint_plus_tail_and_full_replay_keep_same_incremental_ledger(
    openai_provider_config, tmp_path,
):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    controller.create_user_message("验证请求账本的增量落盘")
    assistant = controller.create_assistant_message()
    first = _report(controller, 1, message_id=assistant.id)
    controller.flush()
    controller._sessions.checkpoint()
    repository = ProjectSessionRepository(tmp_path)
    checkpoint = repository.load_checkpoint(controller.session_id)
    assert checkpoint["state"]["state"]["request_usage"] == {first["request_id"]: first}
    second = _report(controller, 2, status="cancelled", message_id=assistant.id)
    controller.set_work_phase("plan")
    third = _report(controller, 3, status="failed", message_id=assistant.id)
    controller.set_work_phase("execute")
    controller.complete_message(assistant.id)
    controller.flush()
    expected = controller._snapshot()
    session_id, paths = controller.session_id, controller.paths
    # 保留旧 checkpoint，刻意让后两次请求只能从尾事件重放出来。
    controller._sessions.close()

    events = repository.read(session_id)
    tail = events[checkpoint["last_seq"]:]
    assert {event["data"]["request_id"] for event in tail if event["type"] == "usage.request_updated"} == {
        second["request_id"], third["request_id"],
    }
    assert all("request_usage" not in event["data"] for event in events if event["type"] == "state.changed")
    service = SessionService(tmp_path)
    prepared = service.prepare(session_id)
    try:
        assert prepared[1] == expected
    finally:
        prepared[0].close()
    paths.checkpoint.unlink()
    prepared = service.prepare(session_id)
    try:
        assert prepared[1] == expected
        assert len(prepared[2][0].request_usage) == 3
    finally:
        prepared[0].close()


@pytest.mark.parametrize("with_checkpoint", [False, True])
def test_recovered_running_request_is_durable_increment_without_importing_old_run(
    openai_provider_config, tmp_path, with_checkpoint,
):
    original_tracker = RunUsageTracker()
    controller = SessionController(openai_provider_config, cwd=tmp_path, usage_tracker=original_tracker)
    controller.create_user_message("模拟供应商上报后程序中断")
    assistant = controller.create_assistant_message()
    running = _report(controller, 1, status="running", message_id=assistant.id)
    controller.flush()
    if with_checkpoint:
        controller._sessions.checkpoint()
    session_id = controller.session_id
    controller._sessions.close()

    new_tracker = RunUsageTracker()
    restored = SessionController(openai_provider_config, cwd=tmp_path, usage_tracker=new_tracker)
    try:
        restored.resume_session(session_id)
        expected = copy.deepcopy(running)
        expected["status"] = "incomplete"
        expected["usage"]["is_final"] = False
        assert restored.state.request_usage == {running["request_id"]: expected}
        assert restored.state.messages[-1].usage.input_tokens == 17
        assert restored.state.messages[-1].usage.output_tokens == 3
        assert restored.state.messages[-1].status == "cancelled"
        assert restored.usage_summary().incomplete_request_count == 1
        assert new_tracker.snapshot().request_count == 0
        events = ProjectSessionRepository(tmp_path).read(session_id)
        updates = [event for event in events if event["type"] == "usage.request_updated"]
        assert [event["data"]["status"] for event in updates] == ["running", "incomplete"]
        assert updates[-1]["turn_id"] == running["turn_id"]
        # 关闭写入者而不更新 checkpoint，确认恢复修复已独立进入事实日志。
        restored._sessions.close()
    finally:
        restored._sessions.close()

    service = SessionService(tmp_path)
    prepared = service.prepare(session_id)
    try:
        assert prepared[1]["state"]["request_usage"] == {running["request_id"]: expected}
        assert prepared[2][0].messages[-1].usage.input_tokens == 17
    finally:
        prepared[0].close()
