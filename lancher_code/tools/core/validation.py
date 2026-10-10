from __future__ import annotations

from dataclasses import dataclass
import json

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, best_match
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable


@dataclass(frozen=True, slots=True)
class ToolValidationIssue:
    error_code: str
    error_message: str
    path: tuple[str | int, ...] = ()
    constraint: str | None = None


def _offline_resource(uri: str):
    """工具输入定义不能让一次本地参数校验变成隐式网络请求。"""
    raise NoSuchResource(ref=uri)


def _display_path(path) -> str:
    result = "$"
    for part in path:
        result += f"[{part}]" if isinstance(part, int) else f"[{json.dumps(part, ensure_ascii=False)}]"
    return result


def validate_tool_arguments(arguments: object, schema: object) -> ToolValidationIssue | None:
    """使用完整 JSON Schema Draft 校验，不通过手写规则猜测 MCP 参数。"""
    if not isinstance(arguments, dict):
        return ToolValidationIssue("invalid_arguments", "工具参数必须是 JSON 对象。")
    try:
        # JSON Schema 讨论的是 JSON 值；NaN/Infinity 等 Python 值不进入工具。
        json.dumps(arguments, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return ToolValidationIssue("invalid_arguments", "工具参数包含不是合法 JSON 的内容。")
    try:
        declared = isinstance(schema, dict) and "$schema" in schema
        validator_class = validator_for(schema, default=None if declared else Draft202012Validator)
        if validator_class is None:
            return ToolValidationIssue("invalid_schema", "工具参数定义使用了不支持的 JSON Schema Draft。")
        validator_class.check_schema(schema)
        validator = validator_class(schema, registry=Registry(retrieve=_offline_resource))
        error = best_match(validator.iter_errors(arguments))
        if error is None:
            return None
        location = _display_path(error.absolute_path)
        keyword = error.validator
        if keyword == "required":
            missing = [key for key in error.validator_value if key not in error.instance]
            explanation = "缺少必填字段：" + "、".join(missing[:8])
        elif keyword == "type":
            expectation = error.validator_value
            explanation = "类型必须为 " + (" 或 ".join(expectation) if isinstance(expectation, list) else expectation)
        elif keyword == "additionalProperties":
            explanation = "包含工具未声明的字段"
        else:
            # 复杂组合规则的字段路径和关键字仍然可定位，实际规则由标准校验器判断。
            explanation = f"不符合 {keyword} 约束"
        return ToolValidationIssue("invalid_arguments", f"参数 {location} {explanation}。",
                                   tuple(error.absolute_path), str(keyword))
    except SchemaError as exc:
        return ToolValidationIssue("invalid_schema", f"工具参数定义无效，位置 {_display_path(exc.absolute_path)}。")
    except Unresolvable:
        return ToolValidationIssue("invalid_schema", "工具参数定义包含无法解析的引用；校验不会访问网络，当前调用未执行。")
    except (TypeError, ValueError, RecursionError):
        return ToolValidationIssue("invalid_schema", "工具参数定义无法完成校验，当前调用未执行。")
