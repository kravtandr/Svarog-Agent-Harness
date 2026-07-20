"""Режим подсказок ввода: IDLE / SLASH / AT (паттерн qwen-code).

Референс: QwenLM/qwen-code `useCommandCompletion` — меню появляется только
когда курсор в токене `/…` (слэш-команда в начале строки) или `@…` (файл).
В IDLE подсказок нет. Логика без TUI — тестируется отдельно от prompt_toolkit.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from svarog_harness.cli.chat_commands import COMMANDS

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        ".svarog",
        ".idea",
        ".vscode",
    }
)
_MAX_FILES = 800
_MAX_SUGGESTIONS = 12


class CompletionMode(StrEnum):
    IDLE = "idle"
    SLASH = "slash"
    AT = "at"


@dataclass(frozen=True)
class Suggestion:
    value: str  # что вставить в буфер
    label: str  # левая колонка меню
    description: str = ""  # правая колонка


@dataclass(frozen=True)
class CompletionQuery:
    mode: CompletionMode
    token: str  # `/he` или `@src/a`


def detect_completion(text_before_cursor: str) -> CompletionQuery:
    """Определить режим по тексту слева от курсора (как qwen-code)."""
    text = text_before_cursor
    if not text:
        return CompletionQuery(CompletionMode.IDLE, "")

    # Токен от последнего пробела — приоритет у @ (можно писать `@file` после текста).
    idx = len(text) - 1
    while idx >= 0 and text[idx] not in " \t\n":
        idx -= 1
    token = text[idx + 1 :]
    if token.startswith("@"):
        return CompletionQuery(CompletionMode.AT, token)

    # Слэш-команды — только если строка начинается с `/` и курсор ещё в первом токене.
    stripped = text.lstrip()
    if stripped.startswith("/") and "\n" not in text and " " not in stripped:
        return CompletionQuery(CompletionMode.SLASH, stripped)

    return CompletionQuery(CompletionMode.IDLE, "")


def slash_suggestions(token: str) -> list[Suggestion]:
    """Подсказки `/`-команд: label + description (как SuggestionsDisplay qwen-code)."""
    if not token.startswith("/"):
        return []
    prefix = token[1:].lower()
    out: list[Suggestion] = []
    for cmd in COMMANDS:
        if cmd.name.startswith(prefix):
            out.append(Suggestion(value=f"/{cmd.name}", label=f"/{cmd.name}", description=cmd.help))
        if len(out) >= _MAX_SUGGESTIONS:
            break
    return out


def list_workspace_files(root: Path, *, limit: int = _MAX_FILES) -> list[str]:
    """Относительные пути файлов workspace (без тяжёлых каталогов)."""
    root = root.resolve()
    found: list[str] = []
    if not root.is_dir():
        return found
    for dirpath, dirnames, filenames in root.walk(top_down=True):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        rel_dir = dirpath.relative_to(root)
        for name in sorted(filenames):
            if name.startswith(".") and name not in {".env.example"}:
                continue
            rel = name if rel_dir == Path() else str(rel_dir / name)
            found.append(rel.replace("\\", "/"))
            if len(found) >= limit:
                return found
    return found


def at_suggestions(root: Path, token: str, *, limit: int = _MAX_SUGGESTIONS) -> list[Suggestion]:
    """Подсказки `@file` по префиксу/подстроке пути."""
    if not token.startswith("@"):
        return []
    query = token[1:].lower()
    files = list_workspace_files(root)
    scored: list[tuple[int, str]] = []
    for path in files:
        lower = path.lower()
        base = Path(path).name.lower()
        if not query:
            scored.append((2, path))
        elif base.startswith(query):
            scored.append((0, path))
        elif lower.startswith(query):
            scored.append((1, path))
        elif query in lower:
            scored.append((3, path))
    scored.sort(key=lambda item: (item[0], item[1]))
    out: list[Suggestion] = []
    for _, path in scored[:limit]:
        out.append(Suggestion(value=f"@{path}", label=path, description="файл"))
    return out
