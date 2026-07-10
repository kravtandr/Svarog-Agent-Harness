"""Применение MemoryChangeRequest к файлам memory-репозитория (ADR-0004).

Пути ограничены корнем memory (тот же jail, что у файловых tools).
replace_section требует стабильных markdown-якорей — заголовков секций.
"""

from datetime import date
from pathlib import Path

from svarog_harness.common.frontmatter import render, split_frontmatter
from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.project_page import project_slug_from_path, stamp_dates


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


def _update_field(text: str, field: str, value: str) -> str:
    """Обновить одно поле YAML-frontmatter, не трогая тело и порядок полей.

    Поля нет во frontmatter → добавляется. Нет frontmatter вовсе →
    MemoryApplyError (обновлять нечего). created/updated ведёт код —
    их через update_field не трогаем.
    """
    if field in ("created", "updated"):
        raise MemoryApplyError(f"поле '{field}' ведёт код, его нельзя менять через update_field")
    frontmatter, body = split_frontmatter(text)
    if not frontmatter:
        raise MemoryApplyError("во frontmatter нечего обновлять: нет валидного ---блока")
    frontmatter[field] = value
    return render(frontmatter, body)


def _new_content(existing: str, request: MemoryChangeRequest) -> str:
    """Содержимое файла после применения create/append/replace_section/update_field."""
    if request.operation is MemoryOperation.CREATE:
        return request.content
    if request.operation is MemoryOperation.APPEND:
        base = existing
        if base and not base.endswith("\n"):
            base += "\n"
        return base + request.content
    if request.operation is MemoryOperation.REPLACE_SECTION:
        return _replace_section(existing, request.section, request.content)
    if request.operation is MemoryOperation.UPDATE_FIELD:
        return _update_field(existing, request.field, request.content)
    raise MemoryApplyError(f"неприменимая операция для расчёта содержимого: {request.operation}")


def preview_content(memory_dir: Path, request: MemoryChangeRequest) -> str:
    """Прогнозируемое содержимое файла после применения — для ранней валидации.

    Без записи на диск и без штамповки дат. DELETE → пустая строка.
    """
    target = resolve_memory_path(memory_dir, request.file)
    if request.operation is MemoryOperation.DELETE:
        return ""
    if (
        request.operation in (MemoryOperation.REPLACE_SECTION, MemoryOperation.UPDATE_FIELD)
        and not target.exists()
    ):
        raise MemoryApplyError(f"файл '{request.file}' не существует для {request.operation.value}")
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    return _new_content(existing, request)


def apply_change(
    memory_dir: Path, request: MemoryChangeRequest, *, today: date | None = None
) -> None:
    """Применить одну заявку к файлам memory (без git-коммита).

    Для страниц проекта (`projects/<slug>/overview.md`) даты created/updated
    ведёт код: `stamp_dates` проставляется при записи (ADR-0011).
    """
    target = resolve_memory_path(memory_dir, request.file)

    if request.operation is MemoryOperation.DELETE:
        target.unlink(missing_ok=True)
        return

    if (
        request.operation in (MemoryOperation.REPLACE_SECTION, MemoryOperation.UPDATE_FIELD)
        and not target.exists()
    ):
        raise MemoryApplyError(f"файл '{request.file}' не существует для {request.operation.value}")

    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    content = _new_content(existing, request)
    if project_slug_from_path(request.file) is not None:
        content = stamp_dates(content, today=today or date.today())

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
