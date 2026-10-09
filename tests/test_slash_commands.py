import pytest

from lancher_code.slash_commands import SlashCompletionContext, create_default_slash_command_registry


def complete(text, **kwargs):
    return create_default_slash_command_registry().complete(SlashCompletionContext(text, **kwargs))


def test_root_only_has_commands_and_supports_chinese_intent():
    rows = complete("/")
    assert [r.display for r in rows] == ["discuss", "plan", "do", "session", "model", "permissions", "compact", "settings", "status", "exit"]
    assert all("<" not in r.display and "[" not in r.display for r in rows)
    assert complete("/切换对话")[0].apply("/切换对话") == "/session "
    assert create_default_slash_command_registry().parse_submission("/mode plan") is None


@pytest.mark.parametrize("text", ["/session", "/session "])
def test_exact_parent_shows_next_level(text):
    rows = complete(text)
    assert [r.display for r in rows] == ["list", "save", "resume", "rename", "remove"]
    assert rows[1].apply(text) == "/session save "


def test_dynamic_names_model_labels_and_optional_force():
    names = ("当前", "旧对话")
    assert [r.display for r in complete("/session remove ", session_names=names, active_session_name="当前")] == ["旧对话"]
    assert complete("/session rename 旧", session_names=names)[0].apply("/session rename 旧") == "/session rename 旧对话 "
    for action in ("save", "resume"):
        rows = complete(f"/session {action} 旧对话 ", session_names=names)
        assert rows[0].display == "--force" and rows[0].optional
        assert not complete(f"/session {action} 旧对话 --f")[0].optional
    row = complete("/model 日常", model_choices=(("provider/code", "日常编程"),), active_model_ref="provider/code", default_model_ref="provider/code")[0]
    assert row.apply("/model 日常") == "/model provider/code"
    assert "本次" in row.description and "默认" in row.description


def test_free_argument_hints_and_tab_advance():
    registry = create_default_slash_command_registry()
    assert "输入一个会话名" in registry.hint("/session save ")
    assert "新的会话名" in registry.hint("/session rename old ")
    assert registry.advance_text("/session save 名称") == "/session save 名称 "
    assert registry.advance_text("/session save ") is None
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
    ("settings", "theme blue"), ("settings", "theme"), ("settings", "open extra"),
    ("model", "one two"), ("status", "extra"), ("mode", "plan"),
])
def test_invalid_commands_are_rejected(name, args):
    with pytest.raises(ValueError):
        create_default_slash_command_registry().validate(name, args)


def test_phase_payload_is_not_tokenized_or_rewritten():
    match = create_default_slash_command_registry().parse_submission('/plan 调查 "多个 空格"\n下一段')
    assert match.arguments_text == '调查 "多个 空格"\n下一段'
