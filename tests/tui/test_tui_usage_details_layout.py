"""详情必须经过真实终端裁剪与滚动，完整数据存在于 Static 还不够。"""
from __future__ import annotations

from xml.etree import ElementTree

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Static

from lancher_code.usage.models import MessageUsage
from lancher_code.config.models import UIConfig
from lancher_code.usage.ledger import RequestUsageRecord
from lancher_code.tui.chat_controls import ReadOnlyDetailsScreen
from lancher_code.tui.composer import ComposerTextArea
from test_tui_flow import FakeProvider, _build_app


def rendered_text(app) -> str:
    """SVG 导出来自屏幕实际渲染，不包含被视口裁掉的后续文本。"""
    root = ElementTree.fromstring(app.export_screenshot())
    return "\n".join("".join(element.itertext()) for element in root.iter()
                     if element.tag.rsplit("}", 1)[-1] == "text")


@pytest.mark.parametrize("size", [(32, 16), (80, 24), (120, 40)])
@pytest.mark.parametrize("entry", ["button", "status"])
async def test_usage_details_are_scrollable_and_leave_the_composer_reachable(
    openai_provider_config, tmp_path, monkeypatch, size, entry,
):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    session.create_user_message("查看包含全部用量子项的对话")
    assistant = session.create_assistant_message()
    request = session.bind_usage_request(session.build_request([], allow_tool_calls=True), message_id=assistant.id)
    request.usage_callback(RequestUsageRecord(
        request_id=request.request_id, run_id=request.run_id, protocol="openai", model=request.model,
        session_id=session.session_id, message_id=assistant.id, status="completed",
        usage=MessageUsage(input_tokens=100, output_tokens=20, cached_input_tokens=40,
                           cache_creation_input_tokens=10, reasoning_output_tokens=5),
    ).to_dict())
    # 布局检查不启动真实进程，但必须让底部的进程数字参与渲染。
    monkeypatch.setattr(app._turn_runner, "execution_summary", lambda: {
        "running": 2, "background": 1, "waiting": 3, "notifications": 4,
    })
    try:
        async with app.run_test(size=size) as pilot:
            composer = app.query_one(ComposerTextArea)
            composer.text = "还没发送的草稿"
            composer.cursor_location = composer.document.end
            await pilot.pause()
            if entry == "button":
                await pilot.click("#chat-details")
            else:
                await app._dispatch_slash_command("status", "")
            await pilot.pause()

            if size[1] < 24:
                assert isinstance(app.screen, ReadOnlyDetailsScreen)
                viewport = app.screen.query_one("#read-only-scroll", VerticalScroll)
            else:
                viewport = app.query_one("#status-details-scroll", VerticalScroll)
                assert viewport.display and app._details_open
                assert 0 < viewport.region.height <= 8
                assert composer.region.bottom <= size[1]
            assert app.focused is viewport
            assert viewport.max_scroll_y > 0
            # 保留原查询接口，正文高度由滚动视口约束，不能再次被 Static 截断。
            assert "缓存比：40.0%" in str(app.query_one("#status-details", Static).render())

            seen = set()
            for _ in range(40):
                text = rendered_text(app)
                seen.update(label for label in ("缓存比", "当前上下文", "托管进程") if label in text)
                if len(seen) == 3:
                    break
                await pilot.press("pagedown")
                await pilot.pause(0.1)
            assert seen == {"缓存比", "当前上下文", "托管进程"}
            await pilot.press("end")
            await pilot.pause()
            assert viewport.scroll_y == viewport.max_scroll_y
            bottom = rendered_text(app)
            assert "未读完成通知" in bottom
            (tmp_path / f"details-{size[0]}x{size[1]}-{entry}.svg").write_text(
                app.export_screenshot(), encoding="utf-8",
            )

            if size[1] < 24:
                await pilot.press("escape")
            else:
                # 内联阅读时输入区仍能立即编辑，不要求先把详情关掉。
                composer.focus()
                await pilot.press("x")
                assert composer.text == "还没发送的草稿x"
                assert viewport.display
                await pilot.click("#chat-details")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert not app.query_one("#status-details-scroll").display
            assert not composer.disabled
            assert 0 <= composer.region.y < composer.region.bottom <= size[1]
            composer.focus()
            if size[1] < 24:
                await pilot.press("x")
            assert composer.text == "还没发送的草稿x"
    finally:
        session.close()
