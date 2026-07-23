"""Memory proposal (блок C, ADR-0020): отложенная пачка правок под ревью.

Dream не пишет в память — он предлагает связный замысел, который применяется
только после решения человека. Один вызов инструмента = один proposal = один
замысел, возможно из нескольких правок.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from svarog_harness.memory.apply import MemoryApplyError, resolve_memory_path
from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.validate import validate_change


@dataclass(frozen=True)
class MemoryProposalRequest:
    title: str
    rationale: str
    changes: tuple[MemoryChangeRequest, ...] = field(default_factory=tuple)
    source_run_id: str | None = None

    def to_changes_json(self) -> list[dict[str, Any]]:
        return [change.to_dict() for change in self.changes]


def _delete_allowed(memory_dir: Path, request: MemoryChangeRequest) -> str | None:
    """delete проходит только для пустого (или отсутствующего) файла.

    Перенос инварианта ADR-0009 «никогда не удаляет — только архивирует» в
    память: для «проект закончился» есть update_field status=archived, страница
    при этом остаётся и уходит в раздел архива index.md (ADR-0011). Правило
    живёт в коде, а не в промте: модель не должна иметь возможности обойти его
    собственной интерпретацией.
    """
    try:
        target = resolve_memory_path(memory_dir, request.file)
    except MemoryApplyError as exc:
        return str(exc)
    if not target.exists():
        return None
    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Нечитаемый файл содержательным не считаем — удалить его законно.
        return None
    if content.strip():
        return (
            f"удаление непустой страницы '{request.file}' запрещено; "
            f"чтобы вывести её из оборота, поставь status: archived через update_field"
        )
    return None


def validate_proposal(memory_dir: Path, request: MemoryProposalRequest) -> list[str]:
    """Проверить proposal целиком; пустой список — валиден."""
    errors: list[str] = []
    if not request.title.strip():
        errors.append("title обязателен: он единственное, что видно в списке на ревью")
    if not request.rationale.strip():
        errors.append(
            "rationale обязателен: человек должен видеть, зачем правка, а не только что она делает"
        )
    if not request.changes:
        errors.append("proposal не содержит ни одной правки")
        return errors

    for index, change in enumerate(request.changes, start=1):
        if change.operation is MemoryOperation.DELETE:
            error = _delete_allowed(memory_dir, change)
        else:
            error = validate_change(memory_dir, change)
        if error is not None:
            errors.append(f"правка {index}: {error}")
    return errors
