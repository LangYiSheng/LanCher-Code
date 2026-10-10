"""命令补全控制器：独立维护候选与选中项，只操作输入相关控件。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
from rich.text import Text
from textual.geometry import Size
from lancher_code.errors import LanCherError
from lancher_code.providers.catalog import iter_model_refs, model_display_name
from lancher_code.sessions.controller import SessionController
from lancher_code.agent.runner import TurnRunner
from lancher_code.tui.commands import (SessionCompletionChoice, SlashCompletionCandidate,
    SlashCompletionContext, SlashCommandRegistry)
from lancher_code.tui.composer import ComposerTextArea, SlashCompletionMenu, CommandHintBar
from lancher_code.tui.tasks import task_label

@dataclass(frozen=True)
class CompletionWidgets:
    composer: ComposerTextArea
    menu: SlashCompletionMenu
    hint: CommandHintBar

class CompletionController:
    def __init__(self, registry: SlashCommandRegistry, session: SessionController, runner: TurnRunner, *,
                 widgets: Callable[[], CompletionWidgets], size: Callable[[], Size], busy: Callable[[], bool],
                 fit_panels: Callable[[], None], refresh_chrome: Callable[[], None]) -> None:
        self.registry, self.session, self.runner = registry, session, runner
        self.widgets, self.size, self.busy = widgets, size, busy
        self.fit_panels, self.refresh_chrome = fit_panels, refresh_chrome
        self.matches: list[SlashCompletionCandidate] = []
        self.index = 0

    async def refresh(self) -> None:
        composer = self.widgets().composer
        menu = self.widgets().menu
        hint_bar = self.widgets().hint
        composer.clear_accepted_slash_command_if_needed()
        if self.busy() and not self.text_allowed_while_busy(composer.text):
            self.matches = []
            self.index = 0
            composer.slash_menu_active = False
            await menu.set_candidates([], None)
            hint_bar.set_hint("本轮结束后可执行命令 · 草稿已保留" if composer.text.lstrip().startswith("/") else "")
            self.refresh_chrome()
            return

        cursor_at_end = composer.cursor_location == composer.document.end
        processes = self.runner.list_processes() if composer.text.lstrip().startswith("/tasks") else []
        sessions = []
        session_listing_error = None
        if cursor_at_end and composer.text.lstrip().startswith("/session"):
            try:
                listing = self.session.list_sessions()
                sessions = listing.items
                if listing.issues:
                    session_listing_error = f"{len(listing.issues)} 个会话不可恢复，/session list 查看详情"
            except (LanCherError, ValueError, OSError) as exc:
                session_listing_error = "会话列表不可用：" + str(exc)
        matches = (
            self.registry.complete(
                SlashCompletionContext(
                    text=composer.text,
                    session_choices=tuple(SessionCompletionChoice(
                        item.session_id, item.title, item.updated_at, item.archived,
                    ) for item in sessions),
                    active_session_id=self.session.session_id,
                    process_choices=tuple((str(item["process_id"]), task_label(item)) for item in processes),
                    model_choices=self.model_choices(),
                    active_model_ref=self.runner.model_ref,
                    default_model_ref=self.runner.model_config.default_model if self.runner.model_config is not None else None,
                    permission_policy=self.session.permission_policy,
                )
            )
            if cursor_at_end and not composer.should_suppress_slash_menu()
            else []
        )
        active_key = self.active_key()
        match_keys = [candidate.key for candidate in matches]
        if active_key in match_keys:
            self.index = match_keys.index(active_key)
        else:
            self.index = 0
        self.matches = matches
        active_key = self.active_key()
        await menu.set_candidates(matches, active_key)
        menu.styles.max_height = 4 if self.size().height < 24 else 7
        composer.slash_menu_active = bool(matches)
        self.fit_panels()
        composer.slash_enter_accepts = not matches or not all(item.optional for item in matches)
        if matches and active_key is not None:
            active = matches[self.index]
            hint_bar.set_hint(session_listing_error or self.hint(active))
            self.refresh_chrome()
            return

        self.matches = []
        self.index = 0
        composer.slash_menu_active = False

        hint_bar.set_hint(session_listing_error or self.registry.hint(composer.text))
        self.refresh_chrome()


    def hint(self, candidate: SlashCompletionCandidate) -> str:
        if candidate.presentation == "session":
            title = Text(candidate.display)
            if self.size().height < 24:
                # 小屏优先保留完整 UUID。长标题可在 /session list 的滚动详情中完整阅读。
                title.truncate(max(1, min(self.size().width, 112) - 4), overflow="ellipsis")
            return title.plain + "\n" + candidate.value
        keys = "Enter 执行 · Tab 填入可选参数" if candidate.optional else "↑↓ 选择 · Tab/Enter 填入 · Esc 关闭"
        detail = candidate.detail or candidate.description
        if self.size().height < 24 and candidate.optional:
            return candidate.description
        if self.size().width < 64 and candidate.presentation != "session":
            detail = candidate.display + " · " + candidate.description
        return detail + " · " + keys


    async def move(self, direction: int) -> None:
        if not self.matches:
            return
        self.index = (self.index + direction) % len(self.matches)
        menu = self.widgets().menu
        await menu.set_candidates(
            self.matches,
            self.active_key(),
        )
        self.widgets().hint.set_hint(self.hint(self.matches[self.index]))


    async def accept_selection(self) -> None:
        candidate_key = self.active_key()
        if candidate_key is None:
            composer = self.widgets().composer
            if (self.busy() and not self.text_allowed_while_busy(composer.text)) or composer.cursor_location != composer.document.end:
                return
            advanced = self.registry.advance_text(composer.text)
            if advanced is not None:
                composer.text = advanced
                composer.cursor_location = composer.document.end
                await self.refresh()
            return
        await self.accept(candidate_key)


    async def accept(self, candidate_key: str) -> None:
        candidate = next(
            (item for item in self.matches if item.key == candidate_key),
            None,
        )
        if candidate is None:
            return

        composer = self.widgets().composer
        composer.text = candidate.apply(composer.text)
        composer.cursor_location = composer.document.end
        if candidate.append_space:
            composer.remember_accepted_slash_command("")
        else:
            composer.remember_accepted_slash_command(composer.text)
        composer.focus()
        await self.refresh()


    async def dismiss(self) -> None:
        self.matches = []
        self.index = 0
        composer = self.widgets().composer
        composer.slash_menu_active = False
        composer.remember_accepted_slash_command(composer.text)
        await self.widgets().menu.set_candidates([], None)
        self.widgets().hint.set_hint("菜单已关闭 · 草稿已保留")
        self.refresh_chrome()


    def active_key(self) -> str | None:
        if not self.matches:
            return None
        if self.index >= len(self.matches):
            self.index = 0
        return self.matches[self.index].key


    @staticmethod
    def allowed_while_busy(command_name: str, arguments_text: str) -> bool:
        return command_name == "tasks" or (command_name == "session" and arguments_text.strip() == "stop")


    def text_allowed_while_busy(self, text: str) -> bool:
        match = self.registry.parse_submission(text)
        return match is not None and self.allowed_while_busy(match.definition.name, match.arguments_text)


    def model_choices(self) -> tuple[tuple[str, str], ...]:
        config = self.runner.model_config
        if config is None:
            return ()
        return tuple((ref, model_display_name(config.providers, ref)) for ref in iter_model_refs(config.providers))
