from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lancher-code",
        description="LanCher Code 交互式对话终端。",
    )
    parser.add_argument(
        "--config",
        default="lancher.yaml",
        help="YAML 配置文件路径，默认使用当前目录下的 lancher.yaml。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    from lancher_code.app import run_app

    try:
        return asyncio.run(run_app(args.config))
    except KeyboardInterrupt:
        return 130
