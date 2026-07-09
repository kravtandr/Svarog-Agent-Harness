"""Применение MemoryChangeRequest к файлам memory-репозитория (ADR-0004).

Пути ограничены корнем memory (тот же jail, что у файловых tools).
replace_section требует стабильных markdown-якорей — заголовков секций.
"""

from pathlib import Path

from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation


class MemoryApplyError(Exception):
    """Заявку нельзя применить (плохой путь, нет секции и т. п.)."""


def resolve_memory_path(memory_dir: Path, raw: str) -> Path:
    """Разрешить относительный путь заявки внутри memory-jail (или MemoryApplyError)."""
    if not raw or raw.startswith("/") or Path(raw).is_absolute():
        raise MemoryApplyError(f"путь памяти должен быть относительным: '{raw}'")
    target = (memory_dir / raw).resolve()
    root = memory_dir.resolve()
    if not target.is_relative_to(root):
        raise MemoryApplyError(f"путь '{raw}' выходит за пределы memory-репозитория")
    return target


def _find_header(lines: list[str], section: str) -> tuple[int, int] | None:
    """Найти строку заголовка секции: (индекс, уровень) или None."""
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") and stripped.lstrip("#").strip() == section:
            return idx, len(stripped) - len(stripped.lstrip("#"))
    return None


def has_section(text: str, section: str) -> bool:
    """Есть ли в markdown-тексте секция с таким заголовком (любого уровня)."""
    return _find_header(text.splitlines(), section) is not None


def _replace_section(text: str, section: str, new_body: str) -> str:
    """Заменить тело markdown-секции (по заголовку любого уровня) на new_body.

    Секция — от строки заголовка до следующего заголовка того же/высшего
    уровня или конца файла. Нет секции → MemoryApplyError.
    """
    lines = text.splitlines()
    header = _find_header(lines, section)
    if header is None:
        raise MemoryApplyError(f"секция '{section}' не найдена в файле")
    header_idx, header_level = header

    end_idx = len(lines)
    for idx in range(header_idx + 1, len(lines)):
        stripped = lines[idx].strip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            if level <= header_level:
                end_idx = idx
                break

    body = new_body.rstrip("\n")
    rebuilt = [*lines[: header_idx + 1], "", body, "", *lines[end_idx:]]
    return "\n".join(rebuilt).rstrip("\n") + "\n"


def apply_change(memory_dir: Path, request: MemoryChangeRequest) -> None:
    """Применить одну заявку к файлам memory (без git-коммита)."""
    target = resolve_memory_path(memory_dir, request.file)

    if request.operation is MemoryOperation.DELETE:
        target.unlink(missing_ok=True)
        return

    if request.operation is MemoryOperation.CREATE:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(request.content, encoding="utf-8")
        return

    if request.operation is MemoryOperation.APPEND:
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        target.write_text(existing + request.content, encoding="utf-8")
        return

    if request.operation is MemoryOperation.REPLACE_SECTION:
        if not target.exists():
            raise MemoryApplyError(f"файл '{request.file}' не существует для replace_section")
        text = target.read_text(encoding="utf-8")
        target.write_text(
            _replace_section(text, request.section, request.content), encoding="utf-8"
        )
        return
