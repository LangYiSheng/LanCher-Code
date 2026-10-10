from __future__ import annotations

import math

import pytest

from lancher_code.models import ToolCall, ToolDefinition, ToolExecutionResult
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.tools.core.validation import validate_tool_arguments


@pytest.mark.parametrize("arguments", [
    {}, {"count": True}, {"count": 0}, {"count": 4, "extra": "no"},
])
def test_standard_schema_rejects_required_types_bounds_and_extra_fields(arguments) -> None:
    schema = {"type": "object", "required": ["count"], "properties": {
        "count": {"type": "integer", "minimum": 1, "maximum": 10}}, "additionalProperties": False}
    issue = validate_tool_arguments(arguments, schema)
    assert issue is not None and issue.error_code == "invalid_arguments"
    assert "参数 $" in issue.error_message


def test_nested_schema_reports_the_actual_field_path() -> None:
    schema = {"type": "object", "properties": {"settings": {"type": "object", "properties": {
        "ports": {"type": "array", "items": {"type": "integer"}}}}}}
    issue = validate_tool_arguments({"settings": {"ports": [8000, "bad"]}}, schema)
    assert issue is not None and issue.path == ("settings", "ports", 1)
    assert '$["settings"]["ports"][1]' in issue.error_message


def test_defs_refs_and_conditional_composition_use_full_standard_validator() -> None:
    schema = {"type": "object", "$defs": {"port": {"type": "integer", "minimum": 1}},
              "properties": {"kind": {"enum": ["service", "file"]}, "port": {"$ref": "#/$defs/port"}},
              "if": {"properties": {"kind": {"const": "service"}}, "required": ["kind"]},
              "then": {"required": ["port"]}}
    assert validate_tool_arguments({"kind": "service", "port": 8000}, schema) is None
    assert validate_tool_arguments({"kind": "service"}, schema).error_code == "invalid_arguments"
    assert validate_tool_arguments({"kind": "service", "port": "wrong"}, schema).path == ("port",)


def test_declared_draft7_tuple_items_are_validated_by_the_declared_draft() -> None:
    schema = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
              "properties": {"tuple": {"type": "array", "items": [{"type": "integer"}, {"type": "string"}]}}}
    assert validate_tool_arguments({"tuple": [1, "value"]}, schema) is None
    assert validate_tool_arguments({"tuple": ["wrong", "value"]}, schema).error_code == "invalid_arguments"


def test_embedded_id_is_resolved_without_external_fetch() -> None:
    schema = {"$id": "https://example.test/root", "type": "object", "$defs": {
        "local": {"$id": "local", "type": "integer"}}, "properties": {"count": {"$ref": "local"}}}
    assert validate_tool_arguments({"count": 5}, schema) is None
    assert validate_tool_arguments({"count": "wrong"}, schema).error_code == "invalid_arguments"


@pytest.mark.parametrize("schema", [
    {"type": "wrong"}, {"$schema": "urn:unsupported-draft"},
    {"$ref": "#/$defs/missing"}, {"$ref": "https://example.test/unavailable.json"},
])
def test_invalid_or_unresolvable_schema_never_becomes_an_execution_permission(schema) -> None:
    issue = validate_tool_arguments({}, schema)
    assert issue is not None and issue.error_code == "invalid_schema"


def test_remote_reference_is_rejected_without_network(monkeypatch) -> None:
    import urllib.request

    def unexpected_network(*_, **__):
        raise AssertionError("参数校验不应请求网络")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected_network)
    issue = validate_tool_arguments({}, {"$ref": "https://example.test/schema"})
    assert issue.error_code == "invalid_schema"


@pytest.mark.parametrize("arguments", [[], {"count": math.nan}, {"value": object()}])
def test_non_json_values_are_rejected(arguments) -> None:
    assert validate_tool_arguments(arguments, {}).error_code == "invalid_arguments"


@pytest.mark.asyncio
async def test_invalid_arguments_do_not_request_approval_resources_or_execute(tmp_path) -> None:
    class CheckedTool:
        definition = ToolDefinition("checked", "", category="command", input_schema={
            "type": "object", "required": ["value"], "properties": {"value": {"type": "integer"}}})

        def resource_claims(self, arguments, context):
            raise AssertionError("坏参数不应到资源声明")

        async def execute(self, arguments, context):
            raise AssertionError("坏参数不应执行")

    registry = ToolRegistry()
    registry.register(CheckedTool())

    async def resolver(request):
        raise AssertionError("坏参数不应请求批准")

    async def started(call):
        raise AssertionError("坏参数不应发出执行开始")

    results = await ToolExecutor(registry, cwd=tmp_path).execute_calls(
        [ToolCall(0, "provider-call", "checked", {"value": "wrong"}, "{}")],
        permission_resolver=resolver, on_call_started=started)
    assert len(results) == 1 and results[0].error_code == "invalid_arguments"


@pytest.mark.asyncio
async def test_invalid_schema_does_not_stop_independent_valid_tools(tmp_path) -> None:
    class BadTool:
        definition = ToolDefinition("bad", "", input_schema={"$ref": "urn:unavailable"})

        async def execute(self, arguments, context):
            raise AssertionError("坏 schema 不应执行")

    class GoodTool:
        definition = ToolDefinition("good", "")

        async def execute(self, arguments, context):
            return ToolExecutionResult("", "good", content="done")

    registry = ToolRegistry()
    registry.register(BadTool())
    registry.register(GoodTool())
    results = await ToolExecutor(registry, cwd=tmp_path).execute_calls([
        ToolCall(0, "bad-id", "bad", {}, "{}"), ToolCall(1, "good-id", "good", {}, "{}")])
    assert results[0].error_code == "invalid_schema" and results[1].ok
