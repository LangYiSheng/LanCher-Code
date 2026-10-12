from __future__ import annotations

import argparse
import asyncio
import math
import sys
from collections.abc import Sequence
from pathlib import Path


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("超时必须是正数秒数。") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("超时必须是有限的正数秒数。")
    return seconds


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lancher",
        description="LanCher Code：终端对话与无界面 Agent 运行。",
    )
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="使用显式配置运行 Agent，不启动界面。")
    run.add_argument("prompt", help="任务内容；使用 - 从标准输入读取。")
    run.add_argument("--config", required=True, type=Path, help="模型配置 YAML 路径。")
    run.add_argument("--cwd", type=Path, help="任务工作目录，默认当前目录。")
    run.add_argument("--model", help="本次模型引用：供应商ID/模型ID。")
    run.add_argument("--session", help="恢复工作目录内的完整会话 UUID。")
    run.add_argument("--format", choices=("text", "json", "jsonl"), default="text", help="输出格式，默认 text。")
    run.add_argument("--timeout", type=_positive_seconds, help="能力初始化与任务处理的总时限，单位秒。")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    from lancher_code.logging_system import close_logging, configure_logging, get_logger

    configure_logging()
    try:
        if args.command == "run":
            from lancher_code.headless import run_headless

            return asyncio.run(run_headless(args))

        from lancher_code.app import run_app

        return asyncio.run(run_app())
    except KeyboardInterrupt:
        return 130
    except ModuleNotFoundError as exc:
        if exc.name == "textual" or exc.name == "rich" or (exc.name or "").startswith(("textual.", "rich.")):
            print('终端界面需要额外依赖，请安装：pip install "lancher-code[tui]"', file=sys.stderr)
            return 1
        get_logger("cli").exception("event=application_uncaught_exception")
        print("启动失败，请查看错误日志。", file=sys.stderr)
        return 1
    except Exception:
        get_logger("cli").exception("event=application_uncaught_exception")
        print("启动失败，请查看错误日志。", file=sys.stderr)
        return 1
    finally:
        close_logging()
