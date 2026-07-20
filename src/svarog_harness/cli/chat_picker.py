"""Интерактивный выбор варианта (↑↓ + Enter) для слэш-команд chat."""

from __future__ import annotations

from collections.abc import Sequence

from prompt_toolkit.shortcuts import radiolist_dialog
from prompt_toolkit.styles import Style

from svarog_harness.cli.chat_display import ACCENT_HEX

_STYLE = Style.from_dict(
    {
        "dialog": "bg:#1a1a1a",
        "dialog.body": "bg:#1a1a1a #d0d0d0",
        "dialog frame.label": f"bg:#1a1a1a {ACCENT_HEX}",
        "dialog.body label": "#d0d0d0",
        "radio-list": "bg:#1a1a1a",
        "radio-selected": f"bg:{ACCENT_HEX} #ffffff",
        "button": f"bg:#1a1a1a {ACCENT_HEX}",
        "button.focused": f"bg:{ACCENT_HEX} #ffffff",
    }
)


async def pick_option(
    title: str,
    values: Sequence[tuple[str, str]],
    *,
    default: str | None = None,
) -> str | None:
    """Показать RadioList; вернуть value или None при Esc/отмене.

    ``values`` — пары ``(value, label)``.
    """
    if not values:
        return None
    keys = {value for value, _ in values}
    selected = default if default in keys else values[0][0]
    return await radiolist_dialog(
        title=title,
        text="↑↓ выбрать · Enter подтвердить · Esc отмена",
        values=list(values),
        default=selected,
        style=_STYLE,
    ).run_async()
