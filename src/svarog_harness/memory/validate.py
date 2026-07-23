"""Единая валидация заявки памяти по текущему состоянию (§6.7, ADR-0011).

Оба пути записи — прямой `remember` и `propose_memory_change` под ревью —
обязаны применять один свод правил. Иначе контракт страницы проекта со
временем разъедется между ними.
"""

from pathlib import Path

from svarog_harness.memory.apply import (
    MemoryApplyError,
    has_section,
    preview_content,
    resolve_memory_path,
)
from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.project_page import project_slug_from_path, validate_project_page


def validate_change(
    memory_dir: Path,
    request: MemoryChangeRequest,
    *,
    pending_files: set[str] | None = None,
) -> str | None:
    """Отловить предсказуемые ошибки применения до постановки в очередь.

    `pending_files` — абсолютные пути, уже поставленные в очередь этим же
    run'ом: очередь применяется после run, поэтому цепочка create →
    replace_section по одному файлу не должна ложно падать на проверке
    существования. None — проверять строго по диску.
    """
    queued = pending_files or set()
    try:
        target = resolve_memory_path(memory_dir, request.file)
    except MemoryApplyError as exc:
        return str(exc)

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
        elif str(target) not in queued:
            # Файл, поставленный в очередь этим же run'ом, ещё не применён —
            # для него проверку пропускаем (оптимистично).
            return f"файл '{request.file}' не существует для replace_section"

    if request.operation is MemoryOperation.UPDATE_FIELD:
        if not request.field:
            return "для update_field нужно указать field (имя поля frontmatter)"
        if not target.exists() and str(target) not in queued:
            return f"файл '{request.file}' не существует для update_field"

    slug = project_slug_from_path(request.file)
    if slug is not None and request.operation is not MemoryOperation.DELETE:
        # Контракт страницы проекта (ADR-0011): frontmatter должен быть валиден
        # в прогнозируемом содержимом. Заявку, поставленную в очередь этим же
        # run'ом и ещё не применённую (нет на диске), пропускаем — она
        # провалидируется своей заявкой.
        if str(target) in queued and not target.exists():
            return None
        try:
            prospective = preview_content(memory_dir, request)
        except MemoryApplyError as exc:
            return str(exc)
        return validate_project_page(prospective, expected_slug=slug)
    return None
