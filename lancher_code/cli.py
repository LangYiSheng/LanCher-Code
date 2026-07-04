from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence


def build_arg_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="lancher",
        description="LanCher Code 终端 AI 对话界面。",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    parser.parse_args(argv)

    from lancher_code.app import run_app

    try:
        return asyncio.run(run_app())
    except KeyboardInterrupt:
        return 130
