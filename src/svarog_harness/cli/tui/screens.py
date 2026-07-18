"""Модальные экраны chat-TUI: approval-гейт, ask_user, выбор сессии, help.

ApprovalScreen показывает фактическое действие (команду/аргументы), не
пересказ (§12) — тот же контент, что `_show_approval` в plain-REPL.
"""

import json
from dataclasses import dataclass
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from svarog_harness.cli.tui.commands import COMMANDS
from svarog_harness.storage.models import Approval
from svarog_harness.trace.viewer import SessionSummary


@dataclass(frozen=True)
class GateDecision:
    """Вердикт человека в модалке гейта (approve/deny или ответ на вопрос)."""

    approved: bool
    reason: str | None = None
    answer: str | None = None


def approval_summary(approval: Approval) -> str:
    """Текст карточки approval: действие, tool, аргументы, причина (§12)."""
    payload = approval.payload or {}
    lines = [
        f"approval {approval.id[:8]} | run {approval.run_id[:8]}",
        f"действие: {approval.action_type}",
    ]
    if payload.get("tool"):
        lines.append(f"tool: {payload['tool']}")
    if payload.get("arguments"):
        lines.append(f"аргументы: {json.dumps(payload['arguments'], ensure_ascii=False, indent=2)}")
    if payload.get("reason"):
        lines.append(f"причина: {payload['reason']}")
    return "\n".join(lines)


class ApprovalScreen(ModalScreen[GateDecision]):
    """Одобрить/отклонить действие агента; при отказе — причина."""

    BINDINGS: ClassVar = [
        Binding("y", "approve", "одобрить"),
        Binding("n", "deny", "отклонить"),
        Binding("escape", "deny", "отклонить", show=False),
    ]

    def __init__(self, approval: Approval) -> None:
        super().__init__()
        self._approval = approval

    def compose(self) -> ComposeResult:
        with Vertical(id="gate-dialog"):
            yield Label("требуется approval", id="gate-title")
            yield Static(approval_summary(self._approval), id="gate-body")
            with Horizontal(id="gate-buttons"):
                yield Button("одобрить (y)", id="gate-approve", variant="success")
                yield Button("отклонить (n)", id="gate-deny", variant="error")
            yield Input(placeholder="причина отказа (Enter — без причины)", id="gate-reason")

    def on_mount(self) -> None:
        self.query_one("#gate-reason", Input).display = False

    def action_approve(self) -> None:
        self.dismiss(GateDecision(approved=True))

    def action_deny(self) -> None:
        # Причина отказа — отдельным полем после выбора «отклонить».
        reason = self.query_one("#gate-reason", Input)
        if reason.display:
            self.dismiss(GateDecision(approved=False))
            return
        reason.display = True
        reason.focus()

    @on(Button.Pressed, "#gate-approve")
    def _on_approve(self) -> None:
        self.action_approve()

    @on(Button.Pressed, "#gate-deny")
    def _on_deny(self) -> None:
        self.action_deny()

    @on(Input.Submitted, "#gate-reason")
    def _on_reason(self, event: Input.Submitted) -> None:
        self.dismiss(GateDecision(approved=False, reason=event.value.strip() or None))


class QuestionScreen(ModalScreen[GateDecision]):
    """ask_user (§6.5): показать вопрос агента и записать текстовый ответ."""

    BINDINGS: ClassVar = [Binding("escape", "skip", "без ответа", show=False)]

    def __init__(self, approval: Approval) -> None:
        super().__init__()
        self._approval = approval

    def compose(self) -> ComposeResult:
        payload = self._approval.payload or {}
        question = str(payload.get("question") or payload.get("reason") or "")
        with Vertical(id="gate-dialog"):
            yield Label(f"вопрос агента | run {self._approval.run_id[:8]}", id="gate-title")
            yield Static(question, id="gate-body")
            yield Input(placeholder="ваш ответ (Enter — продолжить без ответа)", id="gate-answer")

    def on_mount(self) -> None:
        self.query_one("#gate-answer", Input).focus()

    def action_skip(self) -> None:
        self.dismiss(GateDecision(approved=True, answer=""))

    @on(Input.Submitted, "#gate-answer")
    def _on_answer(self, event: Input.Submitted) -> None:
        self.dismiss(GateDecision(approved=True, answer=event.value))


class SessionPickerScreen(ModalScreen[tuple[str, bool] | None]):
    """Выбор сессии: Enter — продолжить, f — форк, Esc — отмена.

    Данные приходят готовыми (summaries + превью по требованию через
    callback приложения) — экран не знает про движок и БД.
    """

    BINDINGS: ClassVar = [
        Binding("enter", "continue_session", "продолжить", priority=True),
        Binding("f", "fork_session", "форк"),
        Binding("escape", "cancel", "отмена"),
    ]

    class PreviewRequested(Message):
        """Просьба приложению подгрузить превью подсвеченной сессии."""

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    def __init__(self, summaries: list[SessionSummary]) -> None:
        super().__init__()
        self._summaries = summaries

    def compose(self) -> ComposeResult:
        options = [
            Option(
                f"{s.session.id[:8]}  {(s.session.title or '')[:40]}  "
                f"[dim]{s.runs} runs | {s.last_task[:40]}[/dim]",
                id=s.session.id,
            )
            for s in self._summaries
        ]
        with Vertical(id="picker-dialog"):
            yield Label("сессии (Enter — продолжить, f — форк, Esc — отмена)", id="gate-title")
            with Horizontal():
                yield OptionList(*options, id="picker-list")
                yield VerticalScroll(Static("", id="picker-preview"), id="picker-preview-pane")

    def on_mount(self) -> None:
        self.query_one("#picker-list", OptionList).focus()

    def _highlighted_id(self) -> str | None:
        option_list = self.query_one("#picker-list", OptionList)
        if option_list.highlighted is None:
            return None
        return option_list.get_option_at_index(option_list.highlighted).id

    @on(OptionList.OptionHighlighted, "#picker-list")
    def _on_highlight(self, event: OptionList.OptionHighlighted) -> None:
        # Превью подгружает приложение (у него движок); экран только просит.
        if event.option.id is not None:
            self.post_message(self.PreviewRequested(event.option.id))

    def show_preview(self, text: str) -> None:
        self.query_one("#picker-preview", Static).update(text)

    def action_continue_session(self) -> None:
        session_id = self._highlighted_id()
        if session_id is not None:
            self.dismiss((session_id, False))

    def action_fork_session(self) -> None:
        session_id = self._highlighted_id()
        if session_id is not None:
            self.dismiss((session_id, True))

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(OptionList.OptionSelected, "#picker-list")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.dismiss((event.option.id, False))


class HelpScreen(ModalScreen[None]):
    """Команды и горячие клавиши."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss_help", "закрыть")]

    _KEYS = (
        ("Esc", "прервать текущий run (будет suspended)"),
        ("Ctrl+Q / Ctrl+C", "выход"),
        ("Ctrl+T", "панель событий"),
        ("Ctrl+S", "выбор сессии"),
        ("Ctrl+N", "новая сессия"),
        ("↑ / ↓", "история ввода"),
    )

    def compose(self) -> ComposeResult:
        lines = ["[bold]слэш-команды[/bold]"]
        lines += [f"  {cmd.usage:<18} {cmd.help}" for cmd in COMMANDS]
        lines.append("")
        lines.append("[bold]клавиши[/bold]")
        lines += [f"  {key:<18} {desc}" for key, desc in self._KEYS]
        with Vertical(id="gate-dialog"):
            yield Label("svarog chat — помощь", id="gate-title")
            yield Static("\n".join(lines), id="gate-body", markup=True)

    def action_dismiss_help(self) -> None:
        self.dismiss(None)
