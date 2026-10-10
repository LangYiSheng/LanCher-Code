import pytest
from datetime import datetime, timedelta, timezone

from lancher_code.slash_commands import SessionCompletionChoice, SlashCompletionContext, create_default_slash_command_registry


UPDATED_AT = datetime(2026, 10, 10, 6, 32, tzinfo=timezone.utc)


def complete(text, **kwargs):
    return create_default_slash_command_registry().complete(SlashCompletionContext(text, **kwargs))


def test_root_only_has_commands_and_supports_chinese_intent():
    rows = complete("/")
    assert [r.display for r in rows] == ["discuss", "plan", "do", "session", "tasks", "model", "permissions", "compact", "settings", "status", "exit"]
    assert all("<" not in r.display and "[" not in r.display for r in rows)
    assert complete("/切换对话")[0].apply("/切换对话") == "/session "
    assert create_default_slash_command_registry().parse_submission("/mode plan") is None


@pytest.mark.parametrize("text", ["/session", "/session "])
def test_exact_parent_shows_next_level(text):
    rows = complete(text)
    assert [r.display for r in rows] == ["new", "list", "stop", "resume", "rename", "archive", "remove"]
    assert rows[0].apply(text) == "/session new"


def test_dynamic_full_ids_model_labels_and_no_force():
    current = "11111111111141118111111111111111"
    other = "22222222222242228222222222222222"
    session_choices = (SessionCompletionChoice(current, "当前任务", UPDATED_AT), SessionCompletionChoice(other, "旧会话", UPDATED_AT))
    for action in ("archive", "remove"):
        assert [r.value for r in complete(f"/session {action} ", session_choices=session_choices, active_session_id=current)] == [other]
    assert complete("/session rename 222", session_choices=session_choices)[0].apply("/session rename 222") == f"/session rename {other} "
    assert complete(f"/session resume {other} ", session_choices=session_choices) == []
    assert complete(f"/session resume {other} --f", session_choices=session_choices) == []
    row = complete("/model 日常", model_choices=(("provider/code", "日常编程"),), active_model_ref="provider/code", default_model_ref="provider/code")[0]
    assert row.apply("/model 日常") == "/model provider/code"
    assert "本次" in row.description and "默认" in row.description


def test_free_argument_hints_and_tab_advance():
    registry = create_default_slash_command_registry()
    assert "完整会话 UUID" in registry.hint("/session resume ")
    assert "会话标题" in registry.hint("/session rename uuid ")
    assert registry.advance_text("/session rename uuid") == "/session rename uuid "
    assert registry.advance_text("/session resume uuid") is None
    assert complete("/session\nresume") == []
    assert complete("普通消息") == []


def test_settings_and_policy_values_are_discoverable():
    assert [r.display for r in complete("/permissions")] == ["default", "acceptEdits", "bypass"]
    assert [r.display for r in complete("/settings theme ")] == ["dark", "light"]
    assert [r.display for r in complete("/settings busy-enter ")] == ["follow_up", "steer", "draft"]
    assert [r.display for r in complete("/settings default-model ", model_choices=(("p/m", "模型"),))] == ["p/m"]


@pytest.mark.parametrize("name,args", [
    ("permissions", "plan"), ("permissions", "default extra"), ("session", "save"),
    ("session", "save name force"), ("session", "rename old"), ("session", "list extra"),
    ("session", "resume uuid --force"), ("session", "new extra"), ("session", "archive"),
    ("session", "remove uuid extra"),
    ("tasks", "unknown"), ("tasks", "stop"), ("tasks", "list extra"),
    ("tasks", "show uuid extra"), ("session", "stop uuid"),
    ("settings", "theme blue"), ("settings", "theme"), ("settings", "open extra"),
    ("model", "one two"), ("status", "extra"), ("mode", "plan"),
])
def test_invalid_commands_are_rejected(name, args):
    with pytest.raises(ValueError):
        create_default_slash_command_registry().validate(name, args)


def test_phase_payload_is_not_tokenized_or_rewritten():
    match = create_default_slash_command_registry().parse_submission('/plan 调查 "多个 空格"\n下一段')
    assert match.arguments_text == '调查 "多个 空格"\n下一段'


def test_session_title_preserves_spaces():
    registry = create_default_slash_command_registry()
    match = registry.parse_submission('/session rename uuid 新标题  保留空格')
    assert match.arguments_text.split(maxsplit=2)[2] == "新标题  保留空格"
    registry.validate("session", match.arguments_text)


def test_session_search_uses_title_words_and_full_replacement_with_stable_identity():
    first = "11111111111141118111111111111111"
    second = "22222222222242228222222222222222"
    choices = (
        SessionCompletionChoice(first, "HTTP 服务测试", UPDATED_AT - timedelta(days=1)),
        SessionCompletionChoice(second, "HTTP 服务测试", UPDATED_AT, archived=True),
    )
    rows = complete("/session resume 服务 HTTP ", session_choices=choices, active_session_id=first)
    assert [row.value for row in rows] == [second, first]
    assert all(row.display == "HTTP 服务测试" and row.presentation == "session" for row in rows)
    assert "已归档" in rows[0].description and second[:8] in rows[0].description
    assert "当前" in rows[1].description and "2026-10-09" in rows[1].description
    assert rows[0].apply("/session resume 服务 HTTP ") == f"/session resume {second}"
    assert f"UUID：{second}" in rows[0].detail and "HTTP 服务测试" in rows[0].detail
    uuid_match = complete("/session resume 222", session_choices=choices)[0]
    assert uuid_match.key == rows[0].key
    renamed = (SessionCompletionChoice(second, "改名后的标题", UPDATED_AT),)
    assert complete("/session resume ", session_choices=renamed)[0].key == rows[0].key
    assert complete("/session resume 未找到", session_choices=choices) == []
    assert complete(f"/session rename {second} 新标题", session_choices=choices) == []


def test_session_completion_preserves_source_order_when_updated_at_equal():
    choices = (
        SessionCompletionChoice("22222222222242228222222222222222", "同名会话", UPDATED_AT),
        SessionCompletionChoice("11111111111141118111111111111111", "同名会话", UPDATED_AT),
    )
    assert [row.value for row in complete("/session resume ", session_choices=choices)] == [choice.session_id for choice in choices]


def test_process_commands_have_discoverable_scope_and_full_ids():
    process_id = "12345678123442348123456781234567"
    rows = complete("/tasks show 123", process_choices=((process_id, "运行中 · 开发服务器"),))
    assert rows[0].apply("/tasks show 123") == f"/tasks show {process_id}"
    assert [row.display for row in complete("/tasks ")] == ["list", "show", "read", "stop", "background"]
    assert "后台进程" in create_default_slash_command_registry().hint("/session stop")
    for arguments in ("", "list", f"read {process_id}", f"background {process_id}"):
        create_default_slash_command_registry().validate("tasks", arguments)
