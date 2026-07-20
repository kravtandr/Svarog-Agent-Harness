"""Ввод chat: сэндвич полос как у Claude Code + меню `/` и `@`.

Раскладка (пока курсор в поле)::

    ▶▶ автономия · executor …
    ─────────────────────────
    › текст
    ─────────────────────────
    /help  описание     ← только при `/` или `@`

Верхняя полоса и статус — в `message`, нижняя — однострочный `bottom_toolbar`
(мультистрочный toolbar у prompt_toolkit часто схлопывается в height=1).
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.history import FileHistory
from prompt_toolkit.shortcuts.prompt import CompleteStyle
from prompt_toolkit.styles import Style

from svarog_harness.cli.chat_completion import (
    CompletionMode,
    at_suggestions,
    detect_completion,
    slash_suggestions,
)
from svarog_harness.cli.chat_display import ACCENT_HEX, ExecutorView
from svarog_harness.config.schema import AutonomyMode

_STYLE = Style.from_dict(
    {
        "status": f"{ACCENT_HEX}",
        "separator": ACCENT_HEX,
        "prompt": f"{ACCENT_HEX} bold",
        # noreverse: иначе toolbar рисуется инверсным и «пропадает» на тёмном фоне
        "bottom-toolbar": f"noreverse {ACCENT_HEX}",
        "bottom-toolbar.text": f"noreverse {ACCENT_HEX}",
        "completion-menu": "bg:#1a1a1a",
        "completion-menu.completion": "bg:#1a1a1a #d0d0d0",
        "completion-menu.completion.current": f"bg:{ACCENT_HEX} #ffffff",
        "completion-menu.meta.completion": "bg:#1a1a1a #888888",
        "completion-menu.meta.completion.current": f"bg:{ACCENT_HEX} #e8e8e8",
    }
)


def _cols() -> int:
    return max(shutil.get_terminal_size((80, 24)).columns, 8)


def _hrule() -> str:
    return "─" * _cols()


class ChatCompleter(Completer):
    """Completer по режимам qwen-code: IDLE → пусто, SLASH → команды, AT → файлы."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    def get_completions(self, document: Document, complete_event: object) -> Iterable[Completion]:
        query = detect_completion(document.text_before_cursor)
        if query.mode is CompletionMode.IDLE:
            return
        token = query.token
        start = -len(token) if token else 0
        if query.mode is CompletionMode.SLASH:
            suggestions = slash_suggestions(token)
        else:
            suggestions = at_suggestions(self._workspace, token)
        for item in suggestions:
            yield Completion(
                item.value,
                start_position=start,
                display=item.label,
                display_meta=item.description,
            )


def _prompt_message(autonomy: AutonomyMode, executor: ExecutorView) -> StyleAndTextTuples:
    """Статус + верхняя полоса + ›  (верх сэндвича Claude Code)."""
    marks = {
        AutonomyMode.YOLO: "▶▶",
        AutonomyMode.AUTO: "▶",
        AutonomyMode.SUPERVISED: "⏸",
    }
    mark = marks[autonomy]
    status = (
        f"{mark} автономия: {autonomy.value}  ·  "
        f"executor: {executor.kind}/{executor.detail} ({executor.role})"
    )
    return [
        ("class:status", status + "\n"),
        ("class:separator", _hrule() + "\n"),
        ("class:prompt", "› "),
    ]


def make_prompt_session(
    workspace: Path,
    history_path: Path,
    autonomy: AutonomyMode,
    executor: ExecutorView,
) -> PromptSession[str]:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    return PromptSession(
        message=lambda: _prompt_message(autonomy, executor),
        history=FileHistory(str(history_path)),
        completer=ChatCompleter(workspace),
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
        style=_STYLE,
        # Одна строка — иначе Dimension(min=1) часто съедает вторую.
        bottom_toolbar=lambda: _hrule(),
        reserve_space_for_menu=8,
        enable_history_search=False,
    )


def prompt_chat_line(session: PromptSession[str]) -> str:
    """Один ввод; EOFError/KeyboardInterrupt пробрасываются вызывающему."""
    return session.prompt()
