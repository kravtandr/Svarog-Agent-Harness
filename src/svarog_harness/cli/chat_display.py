"""Презентация chat/run: welcome как Claude Code, tool-карточки, chrome ввода.

Макет референса (Claude Code / fast-code): двухколоночный welcome (workspace/
статус слева, tips справа), тонкая полоса над промптом, статус внизу. Акцент —
синий (не оранжевый Claude).
Подсказки `/` и `@` — только при наборе (см. chat_completion / chat_prompt).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from svarog_harness import __version__
from svarog_harness.config.schema import AutonomyMode, SvarogConfig

# Синий акцент (референс Claude Code по раскладке, свой цвет).
ACCENT = "dodger_blue2"
ACCENT_HEX = "#1e90ff"
_DIM = "dim"
_MAX_ARG = 72
_BULK_KEYS = frozenset(
    {
        "content",
        "new_string",
        "old_string",
        "newString",
        "oldString",
        "patch",
        "diff",
        "stdout",
        "stderr",
    }
)


def _home_short(path: Path) -> str:
    try:
        return f"~/{path.resolve().relative_to(Path.home().resolve())}"
    except (ValueError, OSError):
        return str(path)


def _first_str(args: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _truncate(text: str, limit: int = _MAX_ARG) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def _size_hint(args: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value:
            n = len(value.encode("utf-8"))
            if n < 1024:
                return f"{n} B"
            return f"{n / 1024:.1f} KB"
    return None


def format_tool_call(name: str, args: dict[str, object]) -> Text:
    """Краткая карточка tool-вызова: путь/команда, без дампа содержимого."""
    key = name.lower().replace("-", "_")
    text = Text()
    text.append("  ")

    if key in {"write", "write_file"}:
        path = _first_str(args, "filePath", "file_path", "path") or "?"
        size = _size_hint(args, "content")
        text.append("✎ ", style=ACCENT)
        text.append("Write ", style=f"bold {ACCENT}")
        text.append(path, style="bold")
        if size:
            text.append(f"  ({size})", style=_DIM)
        return text

    if key in {"edit", "edit_file", "multiedit", "notebookedit"}:
        path = _first_str(args, "filePath", "file_path", "path") or "?"
        text.append("✎ ", style=ACCENT)
        text.append("Edit ", style=f"bold {ACCENT}")
        text.append(path, style="bold")
        return text

    if key in {"read", "read_file"}:
        path = _first_str(args, "filePath", "file_path", "path") or "?"
        text.append("○ ", style=_DIM)
        text.append("Read ", style=_DIM)
        text.append(path)
        return text

    if key in {"bash", "shell", "command_execution"}:
        command = _first_str(args, "command", "cmd") or "?"
        text.append("$ ", style=ACCENT)
        text.append(_truncate(command, 96), style="bold")
        return text

    if key in {"grep", "search_files"}:
        pattern = _first_str(args, "pattern", "query", "regex") or "?"
        scope = _first_str(args, "path", "glob", "include")
        text.append("⌕ ", style=_DIM)
        text.append("Grep ", style=_DIM)
        text.append(_truncate(pattern, 48), style="bold")
        if scope:
            text.append(f"  in {scope}", style=_DIM)
        return text

    if key in {"glob", "list_dir"}:
        pattern = _first_str(args, "pattern", "glob", "path") or "?"
        text.append("▦ ", style=_DIM)
        text.append("Glob ", style=_DIM)
        text.append(pattern)
        return text

    if key in {"webfetch", "web_fetch", "websearch", "web_search"}:
        url = _first_str(args, "url", "query") or "?"
        text.append("↗ ", style=_DIM)
        text.append(name, style=_DIM)
        text.append(" ")
        text.append(_truncate(url, 80))
        return text

    text.append("→ ", style=_DIM)
    text.append(name, style=f"bold {_DIM}")
    summary = _summarize_args(args)
    if summary:
        text.append(" ")
        text.append(summary, style=_DIM)
    return text


def _summarize_args(args: dict[str, object]) -> str:
    parts: list[str] = []
    for key, value in args.items():
        if key in _BULK_KEYS:
            if isinstance(value, str):
                parts.append(f"{key}=<{len(value)} chars>")
            continue
        if isinstance(value, str):
            parts.append(f"{key}={_truncate(value, 40)}")
        elif isinstance(value, (int, float, bool)):
            parts.append(f"{key}={value}")
        elif value is None:
            continue
        else:
            parts.append(f"{key}=…")
        if len(parts) >= 3:
            break
    return " ".join(parts)


@dataclass(frozen=True)
class ExecutorView:
    """Как показать текущий data-plane в UI (ADR-0016)."""

    kind: str  # native | external
    detail: str  # local | docker | claude-code | …
    role: str  # локальный loop | cloud-агент


def executor_view(cfg: SvarogConfig) -> ExecutorView:
    """Тип executor'а и среда: native/local vs external/claude-code и т.п."""
    if cfg.executor.type == "native":
        sandbox = cfg.sandbox.type
        detail = "local" if sandbox == "local-trusted" else sandbox
        return ExecutorView(kind="native", detail=detail, role="локальный loop")
    external = cfg.executor.external
    adapter = external.adapter if external is not None else "external"
    return ExecutorView(kind="external", detail=adapter, role="cloud-агент")


def session_model_label(cfg: SvarogConfig) -> str | None:
    """Модель для welcome: у external — managed model/adapter, иначе models.default."""
    if cfg.executor.type == "external" and cfg.executor.external is not None:
        ext = cfg.executor.external
        if ext.model:
            return ext.model
        return ext.adapter
    provider = cfg.models.providers.get(cfg.models.default)
    return provider.model if provider is not None else None


def input_separator() -> RenderableType:
    """Полоса над зоной ввода (как у Claude Code / qwen-code)."""
    return Rule(style=ACCENT)


def welcome_panel(
    workspace: Path,
    autonomy: AutonomyMode,
    executor: ExecutorView,
    *,
    model: str | None = None,
) -> Panel:
    """Стартовый бокс: слева workspace/статус, справа tips — раскладка Claude Code."""
    info = Text()
    info.append("Welcome\n", style="bold")
    info.append(_home_short(workspace), style="bold")
    info.append("\n")
    info.append(f"{autonomy.value}", style=ACCENT)
    info.append(" · ", style=_DIM)
    info.append(f"{executor.kind}/{executor.detail}", style=ACCENT)
    info.append(f"  ({executor.role})", style=_DIM)
    if model:
        info.append("\n")
        info.append(model, style=_DIM)

    tips = Text()
    tips.append("Tips for getting started\n", style=f"bold {ACCENT}")
    tips.append("/help", style="bold")
    tips.append(" — команды\n", style=_DIM)
    tips.append("/", style=f"bold {ACCENT}")
    tips.append(" — меню команд при наборе\n", style=_DIM)
    tips.append("@", style=f"bold {ACCENT}")
    tips.append(" — подсказки файлов\n", style=_DIM)
    tips.append("Ctrl+C", style="bold")
    tips.append(" — прервать / выход", style=_DIM)

    cols = Table.grid(expand=True, padding=(0, 2))
    cols.add_column(ratio=1)
    cols.add_column(ratio=1)
    cols.add_row(info, tips)

    return Panel(
        cols,
        border_style=ACCENT,
        padding=(0, 1),
        title=f"[bold]Svarog chat[/bold] [dim]v{__version__}[/dim]",
        title_align="left",
        subtitle="[dim]scrollback · без alt-screen[/dim]",
        subtitle_align="right",
    )


def turn_rule() -> RenderableType:
    return Rule(style=_DIM)


def format_user_message(content: str) -> Text:
    text = Text()
    text.append("› ", style=f"bold {ACCENT}")
    text.append(content, style="bold")
    return text
