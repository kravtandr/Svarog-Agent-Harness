"""Виджеты chat-TUI: транскрипт, ввод, статус-бар, панель событий, дропдаун."""

from typing import ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Input, Markdown, OptionList, RichLog, Static
from textual.widgets.markdown import MarkdownStream
from textual.widgets.option_list import Option

from svarog_harness.cli.tui.commands import SlashCommand, complete
from svarog_harness.cli.tui.history import InputHistory

MAX_TURNS = 200  # виджетов в транскрипте; старые удаляются, история — в trace


class Transcript(VerticalScroll):
    """Лента диалога: user-реплики, markdown-ответы агента, служебные заметки."""

    def _trim(self) -> None:
        children = list(self.children)
        for extra in children[: max(0, len(children) - MAX_TURNS)]:
            extra.remove()

    async def add_user(self, text: str) -> None:
        widget = Static(Text(f"› {text}"), classes="user-msg")
        await self.mount(widget)
        self._trim()
        self.anchor()

    async def add_note(self, markup: str, *, classes: str = "note-msg") -> None:
        await self.mount(Static(Text.from_markup(markup), classes=classes))
        self._trim()
        self.anchor()

    async def add_markdown(self, text: str) -> None:
        """Готовый (нестримовый) markdown-блок — реплей истории сессии."""
        await self.mount(Markdown(text, classes="assistant-msg"))
        self._trim()
        self.anchor()

    async def start_assistant(self) -> MarkdownStream:
        """Новый стримовый ответ агента; писать через `MarkdownStream.write`."""
        widget = Markdown("", classes="assistant-msg")
        await self.mount(widget)
        self._trim()
        self.anchor()
        return Markdown.get_stream(widget)

    async def clear_turns(self) -> None:
        await self.remove_children()


class ChatInput(Input):
    """Поле ввода: ↑/↓ — история (или навигация по дропдауну, когда он открыт)."""

    BINDINGS: ClassVar = [
        Binding("up", "history_prev", "история назад", show=False),
        Binding("down", "history_next", "история вперёд", show=False),
        Binding("tab", "accept_completion", "дополнить", show=False),
    ]

    def __init__(self, history: InputHistory, dropdown: "SlashDropdown") -> None:
        super().__init__(placeholder="сообщение агенту, / — команды…", id="chat-input")
        self.history = history
        self._dropdown = dropdown

    def action_history_prev(self) -> None:
        if self._dropdown.display:
            self._dropdown.move_highlight(-1)
            return
        recalled = self.history.prev(self.value)
        if recalled is not None:
            self.value = recalled
            self.cursor_position = len(recalled)

    def action_history_next(self) -> None:
        if self._dropdown.display:
            self._dropdown.move_highlight(1)
            return
        recalled = self.history.next()
        if recalled is not None:
            self.value = recalled
            self.cursor_position = len(recalled)

    def action_accept_completion(self) -> None:
        chosen = self._dropdown.current_command()
        if chosen is not None:
            self.value = f"/{chosen.name} "
            self.cursor_position = len(self.value)


class SlashDropdown(OptionList):
    """Подсказки слэш-команд над полем ввода; виден только при вводе '/…'."""

    def __init__(self) -> None:
        super().__init__(id="slash-dropdown")
        self._matches: list[SlashCommand] = []
        self.display = False

    def refresh_for(self, value: str) -> None:
        matches = complete(value) if value.startswith("/") else []
        self._matches = matches
        if not matches:
            self.display = False
            return
        self.clear_options()
        self.add_options(
            Option(f"{cmd.usage:<18} [dim]{cmd.help}[/dim]", id=cmd.name) for cmd in matches
        )
        self.highlighted = 0
        self.styles.height = min(len(matches), 5)
        self.display = True

    def move_highlight(self, delta: int) -> None:
        if self.option_count == 0:
            return
        current = self.highlighted or 0
        self.highlighted = (current + delta) % self.option_count

    def current_command(self) -> SlashCommand | None:
        if not self.display or self.highlighted is None or not self._matches:
            return None
        if 0 <= self.highlighted < len(self._matches):
            return self._matches[self.highlighted]
        return None


class StatusBar(Static):
    """Нижняя строка: workspace | автономия | сессия | статус run + прогресс."""

    running: reactive[bool] = reactive(False)
    status_text: reactive[str] = reactive("готов")
    progress_text: reactive[str] = reactive("")
    session_label: reactive[str] = reactive("новая сессия")
    hidden_events: reactive[int] = reactive(0)

    _SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, workspace: str, autonomy: str) -> None:
        super().__init__(id="status-bar")
        self._workspace = workspace
        self._autonomy = autonomy
        self._spin = 0

    def on_mount(self) -> None:
        self.set_interval(0.12, self._tick)

    def _tick(self) -> None:
        if self.running:
            self._spin = (self._spin + 1) % len(self._SPINNER)
            self.refresh()

    def render(self) -> Text:
        spinner = f"{self._SPINNER[self._spin]} " if self.running else ""
        parts = [
            self._workspace,
            self._autonomy,
            self.session_label,
            f"{spinner}{self.status_text}",
        ]
        if self.progress_text:
            parts.append(self.progress_text)
        if self.hidden_events:
            parts.append(f"события: {self.hidden_events} (^T)")
        return Text.from_markup("[dim] " + " | ".join(parts) + " [/dim]")


class EventPanel(RichLog):
    """Правая сворачиваемая панель: tool calls, checks, commits, память (§21)."""

    def __init__(self) -> None:
        super().__init__(id="event-panel", markup=True, highlight=False, wrap=True)
        self.display = False

    def log_event(self, markup: str) -> None:
        self.write(Text.from_markup(markup))
