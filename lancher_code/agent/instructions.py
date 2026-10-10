"""项目常驻指令的只读加载，不依赖终端界面。"""
from __future__ import annotations

from html import escape
from pathlib import Path


MAX_INSTRUCTION_BYTES = 32_768


def project_instructions(root: Path) -> str | None:
    root = root.resolve()
    path = root / 'AGENTS.md'
    try:
        if not path.exists():
            return None
        if not path.resolve().is_relative_to(root):
            raise ValueError('AGENTS.md 的链接目标位于项目之外。')
        with path.open('rb') as stream:
            raw = stream.read(MAX_INSTRUCTION_BYTES + 1)
        if len(raw) > MAX_INSTRUCTION_BYTES:
            raise ValueError('AGENTS.md 超过 32 KiB，请缩减项目常驻指令。')
        body = raw.decode('utf-8-sig')
    except (OSError, UnicodeError, ValueError) as exc:
        return f'<project_instructions_issue>{escape(str(exc))}</project_instructions_issue>'
    return (
        '<project_instructions>\n'
        '以下是项目 AGENTS.md 约定；遵循用户当前请求和主机阶段、权限限制。\n'
        f'<source>{escape(str(path))}</source>\n{escape(body)}\n'
        '</project_instructions>'
    ) if body.strip() else None
