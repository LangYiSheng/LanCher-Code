from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from lancher_code.models import StreamEvent

ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
CJK_RE = re.compile(r"[\u3400-\u9FFF]")


def estimate_token_count(text: str) -> int:
    normalized = text.strip()
    if not normalized:
        return 0

    ascii_words = ASCII_WORD_RE.findall(normalized)
    without_ascii = ASCII_WORD_RE.sub("", normalized)
    cjk_chars = CJK_RE.findall(without_ascii)
    remainder = CJK_RE.sub("", without_ascii)
    punctuation = "".join(character for character in remainder if not character.isspace())

    return len(ascii_words) + len(cjk_chars) + math.ceil(len(punctuation) / 2)


@dataclass(slots=True)
class StreamAccumulator:
    text_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    thinking_seen: bool = False
    usage: dict[str, Any] | None = None

    def consume(self, event: StreamEvent) -> None:
        if event.kind == "text_delta" and event.text:
            self.text_parts.append(event.text)
        elif event.kind == "thinking_delta":
            self.thinking_seen = True
            if event.text:
                self.thinking_parts.append(event.text)
        elif event.kind == "message_end":
            usage = event.metadata.get("usage")
            if isinstance(usage, dict):
                self.usage = usage

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    @property
    def thinking(self) -> str:
        return "".join(self.thinking_parts)
