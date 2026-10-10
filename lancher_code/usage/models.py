from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MessageUsage:
    """提供方上报的累计快照；None 是未知，零是确实上报了零。"""

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    is_final: bool = True
    partial_fields: frozenset[str] = frozenset()
    invalid_reasons: tuple[str, ...] = ()

    @property
    def known_fields(self) -> frozenset[str]:
        return frozenset(name for name, attribute in USAGE_FIELD_ATTRIBUTES.items()
                         if getattr(self, attribute) is not None)

    @property
    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = list(self.invalid_reasons)
        for name, attribute in USAGE_FIELD_ATTRIBUTES.items():
            value = getattr(self, attribute)
            if value is not None and (type(value) is not int or value < 0):
                errors.append(f"{name} 用量必须为非负整数。")
        if errors:
            return tuple(errors)
        if self.input_tokens is not None:
            for name, value in (("缓存读取", self.cached_input_tokens),
                                ("缓存创建", self.cache_creation_input_tokens)):
                if value is not None and value > self.input_tokens:
                    errors.append(f"{name}用量超过输入总量。")
            if (self.cached_input_tokens is not None and self.cache_creation_input_tokens is not None
                    and self.cached_input_tokens + self.cache_creation_input_tokens > self.input_tokens):
                errors.append("缓存读取与创建之和超过输入总量。")
        if (self.output_tokens is not None and self.reasoning_output_tokens is not None
                and self.reasoning_output_tokens > self.output_tokens):
            errors.append("推理用量超过输出总量。")
        return tuple(errors)

    @property
    def is_valid(self) -> bool:
        return not self.validation_errors

    @property
    def is_complete(self) -> bool:
        return (self.is_final and self.is_valid
                and {"input", "output"} <= self.known_fields
                and not {"input", "output"} & self.partial_fields)

    def to_dict(self) -> dict[str, object]:
        return {**{attribute: getattr(self, attribute) for attribute in USAGE_FIELD_ATTRIBUTES.values()},
                "is_final": self.is_final, "partial_fields": sorted(self.partial_fields),
                "invalid_reasons": list(self.invalid_reasons)}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> MessageUsage:
        values = {attribute: data.get(attribute) for attribute in USAGE_FIELD_ATTRIBUTES.values()}
        if any(value is not None and (type(value) is not int or value < 0) for value in values.values()):
            raise ValueError("用量字段必须为非负整数或 null。")
        final = data.get("is_final", False)
        partial = data.get("partial_fields", [])
        invalid = data.get("invalid_reasons", [])
        if (type(final) is not bool or not isinstance(partial, list)
                or any(not isinstance(name, str) or name not in USAGE_FIELD_ATTRIBUTES for name in partial)
                or not isinstance(invalid, list) or any(not isinstance(reason, str) for reason in invalid)):
            raise ValueError("用量完整性元数据无效。")
        return cls(**values, is_final=final, partial_fields=frozenset(partial),
                   invalid_reasons=tuple(invalid))  # type: ignore[arg-type]


USAGE_FIELD_ATTRIBUTES = {
    "input": "input_tokens", "output": "output_tokens", "cache": "cached_input_tokens",
    "cache_creation": "cache_creation_input_tokens", "reasoning": "reasoning_output_tokens",
}


def merge_usage(current: MessageUsage, incoming: MessageUsage) -> MessageUsage:
    """同一请求的累计帧替换；缺字段保留，明确上报的零可以覆盖。"""
    values = {attribute: (getattr(incoming, attribute) if getattr(incoming, attribute) is not None
                          else getattr(current, attribute))
              for attribute in USAGE_FIELD_ATTRIBUTES.values()}
    partial = (current.partial_fields - incoming.known_fields) | incoming.partial_fields
    return MessageUsage(**values, is_final=incoming.is_final, partial_fields=partial,
                        invalid_reasons=incoming.invalid_reasons)


def add_usage(*usages: MessageUsage) -> MessageUsage:
    """不同请求只累加已知分量，同时保留有多少统计口径不完整。"""
    if not usages:
        return MessageUsage()
    values: dict[str, int | None] = {}
    partial: set[str] = set()
    for name, attribute in USAGE_FIELD_ATTRIBUTES.items():
        known = [getattr(usage, attribute) for usage in usages if getattr(usage, attribute) is not None]
        values[attribute] = sum(known) if known else None
        if len(known) != len(usages) or any(name in usage.partial_fields for usage in usages):
            partial.add(name)
    return MessageUsage(**values, is_final=all(usage.is_final for usage in usages),
                        partial_fields=frozenset(partial),
                        invalid_reasons=tuple(dict.fromkeys(error for usage in usages
                                                           for error in usage.validation_errors)))
