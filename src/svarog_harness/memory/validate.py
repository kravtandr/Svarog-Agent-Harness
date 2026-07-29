"""Единая валидация заявки памяти по текущему состоянию (§6.7, ADR-0011).

Оба пути записи — прямой `remember` и `propose_memory_change` под ревью —
обязаны применять один свод правил. Иначе контракт страницы проекта со
временем разъедется между ними.
"""

from collections.abc import Mapping
from pathlib import Path

from svarog_harness.memory.apply import (
    MemoryApplyError,
    _new_content,
    has_section,
    resolve_memory_path,
)
from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.project_page import project_slug_from_path, validate_project_page


def validate_change(
    memory_dir: Path,
    request: MemoryChangeRequest,
    *,
    pending_changes: Mapping[str, list[MemoryChangeRequest]] | None = None,
) -> str | None:
    """Отловить предсказуемые ошибки применения до постановки в очередь.

    `pending_changes` — заявки, уже поставленные в очередь этим же run'ом,
    сгруппированные по абсолютному пути файла. Очередь применяется после run,
    поэтому цепочки по одному файлу (create → replace_section, а также
    update_field → update_field) не должны ложно падать. Контракт страницы
    проекта валидируется по *просуммированному* состоянию: queued-заявки
    накатываются на дисковое содержимое через тот же `_new_content`, которым
    пользуется single-writer, — иначе вторая `update_field` в цепочке не видит
    поле, добавленное первой. None — проверять строго по диску.
    """
    pending = dict(pending_changes or {})
    try:
        target = resolve_memory_path(memory_dir, request.file)
    except MemoryApplyError as exc:
        return str(exc)

    if request.file.split("/", 1)[0] == "memory":
        # Пути относительны корню memory/ — лишний префикс memory/ (частая
        # ошибка слабых моделей, находка S30) создал бы осиротевший вложенный
        # файл внутри jail. Отклоняем с подсказкой — модель повторяет верно.
        return (
            f"путь '{request.file}' начинается с 'memory/' — пути уже относительны "
            f"корню памяти, убери префикс (например 'user/profile.md')"
        )

    if request.file.split("/", 1)[0] == "sources" and request.operation in (
        MemoryOperation.APPEND,
        MemoryOperation.REPLACE_SECTION,
        MemoryOperation.UPDATE_FIELD,
    ):
        # sources/ — raw-слой (ADR-0011): исходники неизменяемы, правки
        # запрещены. Нужен новый вариант — create нового файла.
        return (
            f"'{request.file}' в sources/ — неизменяемый исходник; "
            f"правки запрещены, создай новый файл через create"
        )

    if request.operation is MemoryOperation.CREATE and target.exists():
        return (
            f"файл '{request.file}' уже существует; create перезаписывает файл "
            f"целиком — используй append или replace_section"
        )

    if request.operation is MemoryOperation.REPLACE_SECTION:
        if not request.section:
            return "для replace_section нужно указать section"
        if target.exists():
            text = target.read_text(encoding="utf-8")
            if not has_section(text, request.section):
                return (
                    f"секция '{request.section}' не найдена в '{request.file}'; "
                    f"проверь заголовок или используй append"
                )
        elif str(target) not in pending:
            # Файл, поставленный в очередь этим же run'ом, ещё не применён —
            # для него проверку пропускаем (оптимистично).
            return f"файл '{request.file}' не существует для replace_section"

    if request.operation is MemoryOperation.UPDATE_FIELD:
        if not request.field:
            return "для update_field нужно указать field (имя поля frontmatter)"
        if not target.exists() and str(target) not in pending:
            return f"файл '{request.file}' не существует для update_field"

    slug = project_slug_from_path(request.file)
    if (
        request.file.split("/", 1)[0] == "projects"
        and slug is None
        and request.operation is not MemoryOperation.DELETE
    ):
        # Проект обязан жить по canonical-пути projects/<slug>/overview.md
        # (ADR-0011). Любой другой путь под projects/ (projects/<slug>.md,
        # projects/<slug>/notes.md и т.п.) — это не проектная страница:
        # validate_project_page её не проверит, index.md не индексирует,
        # даты не проставятся. Отвергаем с подсказкой canonical-формата, чтобы
        # агент сразу исправил путь, а не записал осиротевший файл (S7/S9).
        return (
            f"проект должен быть в projects/<slug>/overview.md "
            f"(получено '{request.file}'); overview.md — обязательная страница "
            f"проекта с frontmatter (name, slug, summary, status)"
        )
    if slug is not None and request.operation is not MemoryOperation.DELETE:
        # Контракт страницы проекта (ADR-0011): frontmatter должен быть валиден
        # в прогнозируемом содержимом. Просуммируем queued-заявки этого же run'а
        # поверх диска тем же `_new_content`, которым применяет очередь
        # single-writer — иначе цепочка update_field summary → update_field
        # status ложно падает: вторая заявка не видит поле из первой.
        queued_for_file = pending.get(str(target), ())
        try:
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            for change in queued_for_file:
                existing = _new_content(existing, change)
            prospective = _new_content(existing, request)
        except MemoryApplyError as exc:
            return str(exc)
        return validate_project_page(prospective, expected_slug=slug)
    return None
