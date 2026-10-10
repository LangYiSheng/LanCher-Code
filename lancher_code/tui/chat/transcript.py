"""消息时间线视图：维护控件身份、增量投影和会话恢复顺序。"""
from __future__ import annotations
from collections.abc import Callable
from textual.containers import VerticalScroll
from lancher_code.context.models import CompactionActivity
from lancher_code.sessions.models import SessionMessage
from lancher_code.sessions.controller import SessionController
from lancher_code.agent.events import TurnEvent
from lancher_code.tui.message import MessageWidget
from lancher_code.tui.compaction import CompactionActivityWidget

class TranscriptView(VerticalScroll):
    def __init__(self, session: SessionController, show_thinking: Callable[[], bool]) -> None:
        super().__init__(id="chat-view")
        self.session, self.show_thinking = session, show_thinking
        self.message_widgets: dict[str, MessageWidget] = {}
        self.compaction_widgets: dict[str, CompactionActivityWidget] = {}
        self.turn_message_ids: set[str] = set()

    async def mount_message(self, message: SessionMessage) -> None:
        chat_view = self
        widget = MessageWidget(message, show_thinking=self.show_thinking(),
                               compaction_activities=self.session.state.compaction_activities)
        self.message_widgets[message.id] = widget
        await chat_view.mount(widget)

    async def sync_message(self, message_id: str, *, compaction: CompactionActivity | None = None) -> None:
        widget = self.message_widgets[message_id]
        activities = self.session.state.compaction_activities
        if compaction is not None:
            # 开始事件携带自己的快照；事件排队期间 Controller 可能已完成压缩。
            activities = {**activities, compaction.id: compaction}
        await widget.update_from_message(self.session.get_message(message_id),
                                        compaction_activities=activities)

    async def mount_compaction(self, activity: CompactionActivity) -> None:
        widget = CompactionActivityWidget(activity)
        widget.add_class("standalone-compaction")
        self.compaction_widgets[activity.id] = widget
        await self.mount(widget)

    async def restore(self) -> None:
        chat_view = self
        for child in list(chat_view.children):
            await child.remove()
        self.message_widgets.clear()
        self.compaction_widgets.clear()
        self.turn_message_ids.clear()
        messages = self.session.state.messages
        message_ids = {message.id for message in messages}
        standalone: dict[str | None, list[CompactionActivity]] = {}
        for activity in self.session.state.compaction_activities.values():
            if activity.message_id in message_ids:
                continue
            standalone.setdefault(activity.after_message_id, []).append(activity)
        for activity in standalone.pop(None, []):
            await self.mount_compaction(activity)
        for message in messages:
            await self.mount_message(message)
            for activity in standalone.pop(message.id, []):
                await self.mount_compaction(activity)
        for activities in standalone.values():
            for activity in activities:
                await self.mount_compaction(activity)


    async def consume(self, event: TurnEvent) -> None:
        if event.kind == "compaction_updated" and event.compaction is not None:
            activity = event.compaction
            if activity.message_id is not None and activity.message_id in self.message_widgets:
                await self.sync_message(activity.message_id, compaction=activity)
            elif activity.id in self.compaction_widgets:
                self.compaction_widgets[activity.id].update_activity(activity)
            else:
                await self.mount_compaction(activity)
        elif event.message is not None and event.kind in {"user_message_created", "assistant_message_started"}:
            await self.mount_message(event.message)
            if event.kind == "assistant_message_started":
                self.turn_message_ids.add(event.message.id)
        elif event.message is not None:
            await self.sync_message(event.message.id)
        if event.kind == "turn_completed":
            for message_id in self.turn_message_ids:
                widget = self.message_widgets.get(message_id)
                if widget is not None:
                    widget.collapse_for_completion()
