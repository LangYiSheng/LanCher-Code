"""无界面命令行适配器；任务编排与资源生命周期交给 Agent SDK。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import aclosing
from dataclasses import replace

from lancher_code.config.loader import load_config
from lancher_code.logging_system import get_logger, register_sensitive_values, sanitize_log_text
from lancher_code.permissions.models import PermissionResolution
from lancher_code.sdk import AgentResult, create_agent


logger = get_logger("headless")


def _use_utf8(stream) -> None:
    """标准管道固定 UTF-8；宿主的内存和捕获流保留其已有接口。"""
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            # 自定义流可能声明接口但不允许重新配置。
            pass


def _write_json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False), flush=True)


def _write_result(result: AgentResult, output_format: str) -> None:
    if result.error_text:
        result = replace(result, error_text=sanitize_log_text(result.error_text),
                         text=sanitize_log_text(result.text) if result.status != "completed" else result.text)
    if output_format == "json":
        _write_json(result.to_dict())
    elif output_format == "jsonl":
        _write_json({"kind": "result", "result": result.to_dict()})
    elif result.text:
        print(result.text, flush=True)
    if result.error_text:
        print(sanitize_log_text(result.error_text), file=sys.stderr, flush=True)


def _exit_code(result: AgentResult) -> int:
    if result.status == "completed":
        return 0
    if result.status == "cancelled":
        return 130
    if result.status == "timeout":
        return 124
    return 1


async def _stream(agent, prompt: str) -> AgentResult:
    async with aclosing(agent.run_user_turn(prompt)) as events:
        async for event in events:
            _write_json(event.to_dict())
            if event.kind == "permission_request_created" and event.permission_request is not None:
                # 批处理没有人工审批入口；保持核心的阶段和访问范围，不自动提权。
                agent.resolve_permission_request(PermissionResolution(
                    request_id=event.permission_request.request_id, outcome="deny",
                ))
    if agent.last_result is None:
        raise RuntimeError("Agent 未返回任务结果。")
    return agent.last_result


async def run_headless(args: argparse.Namespace) -> int:
    _use_utf8(sys.stdout)
    _use_utf8(sys.stderr)
    agent = None
    try:
        if args.prompt == "-":
            _use_utf8(sys.stdin)
            prompt = sys.stdin.read()
        else:
            prompt = args.prompt
        if not prompt.strip():
            raise ValueError("任务内容不能为空。")
        config = load_config(args.config.expanduser().resolve())
        register_sensitive_values(
            key for provider in config.providers.values()
            for key in (provider.api_key, *(model.api_key for model in provider.models.values()))
        )
        agent = create_agent(config.to_agent_config(), cwd=args.cwd, model_ref=args.model)
        async with asyncio.timeout(args.timeout):
            async with agent:
                try:
                    if args.session:
                        agent.resume_session(args.session)
                        if args.model:
                            agent.switch_model(args.model)
                    result = (await _stream(agent, prompt) if args.format == "jsonl"
                              else await agent.run(prompt))
                except asyncio.CancelledError:
                    # 先等待停止完成，再由上下文管理器关闭 MCP、执行运行时和会话。
                    await agent.stop_and_wait()
                    raise
        _write_result(result, args.format)
        return _exit_code(result)
    except TimeoutError:
        error_text, status, code = "任务达到超时时限，已停止并完成资源清理。", "timeout", 124
    except (asyncio.CancelledError, KeyboardInterrupt):
        error_text, status, code = "任务已取消。", "cancelled", 130
    except Exception as exc:
        logger.exception("event=headless_run_failed")
        error_text, status, code = sanitize_log_text(getattr(exc, "user_message", str(exc))), "failed", 1
    previous = agent.last_result if agent is not None else None
    result = replace(previous, status=status, error_text=error_text) if previous is not None else AgentResult(
        status=status, session_id=agent.session_id if agent is not None else None,
        text="", error_text=error_text,
    )
    _write_result(result, args.format)
    return code
