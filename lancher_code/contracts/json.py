"""公开事件与结果的 JSON 值转换，不输出对象 repr。"""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path


def json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return json_value(value.value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return [json_value(item) for item in sorted(value)]
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    raise TypeError(f"事件中包含不能序列化的类型：{type(value).__name__}")
