from __future__ import annotations

from copy import deepcopy
from collections.abc import Awaitable, Callable
from lancher_code.agent.events import TurnEvent
from lancher_code.contracts.control import WorkPhase, PermissionPolicy, CancellationToken
from lancher_code.contracts.tools import ToolCall, ToolExecutionResult
from lancher_code.execution.contracts import InvocationInfo
from lancher_code.sessions.controller import SessionController
from lancher_code.tools.core.executor import ToolExecutor, PermissionResolver
from lancher_code.tools.core.base import Tool
from lancher_code.tools.core.registry import ToolRegistry


async def execute_batch(*, session: SessionController, executor: ToolExecutor,
                        tool_calls: list[ToolCall], precomputed_results: list[ToolExecutionResult],
                        pending: list[ToolCall], message_id: str, turn_id: str, generation: int,
                        phase: WorkPhase, policy: PermissionPolicy, cancellation_token: CancellationToken,
                        permission_resolver: PermissionResolver, available_tool_names: set[str],
                        expected_tool_bindings: dict[str, Tool],
                        should_interrupt: Callable[[], bool], is_current: Callable[[], bool],
                        emit: Callable[[TurnEvent], Awaitable[None]]) -> list[ToolExecutionResult]:
    """一批调用的事件、结果落盘及幂等回调使用同一个边界。"""
    recorded_ids: set[str] = set()

    async def report_invocation_state(call: ToolCall, info: InvocationInfo) -> None:
        # 只把当前轮次的真实执行投影送到时间线；审批结束不等于工具已启动。
        if (not is_current() or info.turn_id != turn_id
                or info.session_id != session.session_id
                or call.call_id in recorded_ids):
            return
        labels = {
            "queued": "等待执行", "awaiting_permission": "等待批准",
            "waiting_resources": "等待资源 · 审批已通过", "running": "正在执行",
        }
        if info.state not in labels:
            return
        message = session.set_trace_tool_state(
            message_id, call.call_id, info.state,
            waiting=info.waiting, invocation_id=info.invocation_id)
        await emit(TurnEvent(kind="progress_updated", message=message,
            tool_call=call, progress_message=f"{labels[info.state]} · {call.tool_name}"))

    async def report_started(call: ToolCall) -> None:
        message = session.set_trace_tool_state(message_id, call.call_id, "running")
        session.record_event('tool.started', {'call_id': call.call_id, 'tool_name': call.tool_name},
                                   turn_id=turn_id)
        await emit(TurnEvent(kind="progress_updated", message=message,
            tool_call=call, progress_message=f"正在执行 {call.tool_name}"))

    async def report_result(result: ToolExecutionResult) -> None:
        # 回调与返回列表共用幂等入口，取消只补齐尚未收到的结果。
        if result.call_id in recorded_ids:
            return
        recorded_ids.add(result.call_id)
        session.append_tool_results([result])
        pending[:] = [call for call in pending if call.call_id != result.call_id]
        message = session.append_trace_tool_results(message_id, [result])
        session.record_event('tool.finished', {'call_id': result.call_id, 'ok': (not result.is_error)},
                                   turn_id=turn_id)
        await emit(TurnEvent(kind="tool_result_received", message=message,
            usage=deepcopy(session.get_message(message_id).usage), tool_result=result))

    results = precomputed_results or await executor.execute_calls(
        tool_calls,
        work_phase=phase,
        permission_policy=policy,
        plan_file_path=session.plan_file_path,
        session_id=session.session_id,
        session_workspace=session.paths.workspace if session.paths else None,
        session_root=session.paths.root if session.paths else None,
        cancellation_token=cancellation_token,
        turn_id=turn_id,
        generation=generation,
        permission_resolver=permission_resolver,
        available_tool_names=available_tool_names,
        expected_tool_bindings=expected_tool_bindings,
        should_interrupt=should_interrupt,
        on_call_started=report_started,
        on_invocation_state=report_invocation_state,
        on_result=report_result,
    )
    for result in results:
        await report_result(result)
    return results


def next_unknown_tool_streak(current_streak: int, results: list[ToolExecutionResult]) -> int:
    if not results:
        return 0
    streak = current_streak
    for result in results:
        if result.error_code == "tool_not_found":
            streak += 1
        else:
            streak = 0
    return streak


def close_pending_tool_calls(session: SessionController, registry: ToolRegistry, message_id: str, pending: list[ToolCall], reason: str, *, invocation_records: list[dict]) -> None:
    if not pending:
        return
    # 仅补齐尚未记录结果的调用，不能把中断误报为工具完全没有执行。
    trace = session.get_message(message_id).trace.entries
    invocations = {item['invocation_id']: item for item in invocation_records}
    results = []
    for call in pending:
        entry = next((item for item in reversed(trace) if item.kind == 'tool_call' and item.call_id == call.call_id), None)
        started = bool(entry and entry.metadata.get('started'))
        try:
            definition = registry.get(call.tool_name).definition
            external = bool(definition.permission and definition.permission.source == 'external' and definition.category != 'read')
        except Exception:
            external = False
        invocation = invocations.get(entry.metadata.get('invocation_id')) if entry else None
        if (external and invocation and invocation['state'] == 'cancelled'
                and invocation.get('error_code') is None):
            # running 状态通知可先让出控制权。执行器只有真正进入远端
            # execute 后才把取消记为 interrupted / outcome_unknown。
            started = False
            entry.metadata['started'] = False
        # 目录可能已经断线或被刷新，已启动调用的真实分类以执行器记录为准。
        unknown_remote = bool(invocation and invocation.get('error_code') == 'mcp_outcome_unknown') or started and external
        message = (f'{reason}，远端操作结果未知；停止本地等待不能证明远端已撤销。请先检查远端状态，勿直接重复提交。'
                   if unknown_remote else
                   f'{reason}，未获得此工具调用的完整结果。操作可能已部分执行，请先检查当前状态，勿直接重复执行。'
                   if started else f'{reason}，此工具尚未启动，没有执行。')
        results.append(ToolExecutionResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            content=message,
            is_error=True,
            summary='远端结果未知' if unknown_remote else '工具结果未完成' if started else '工具未启动',
            error_code='mcp_outcome_unknown' if unknown_remote else 'tool_result_interrupted',
            error_message=message,
            metadata={'started': started, 'outcome': 'unknown' if started else 'not_started'},
        ))
    session.append_tool_results(results)
    pending.clear()
    session.append_trace_tool_results(message_id, results)
