"""MemoryChangeRequest — структурированная заявка на изменение памяти (ADR-0004).

Run'ы не пишут в memory-репозиторий напрямую: они формируют заявки, которые
единственный writer применяет последовательно.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class MemoryOperation(StrEnum):
    CREATE = "create"  # создать/перезаписать файл целиком
    APPEND = "append"  # дописать в конец
    REPLACE_SECTION = "replace_section"  # заменить содержимое markdown-секции по заголовку
    UPDATE_FIELD = "update_field"  # обновить одно поле YAML-frontmatter, не трогая тело
    DELETE = "delete"  # удалить файл


@dataclass(frozen=True)
class MemoryChangeRequest:
    file: str  # путь относительно корня memory-репозитория
    operation: MemoryOperation
    content: str = ""
    section: str = ""  # заголовок секции для replace_section (без #)
    field: str = ""  # имя поля frontmatter для update_field
    source_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "operation": self.operation.value,
            "content": self.content,
            "section": self.section,
            "field": self.field,
        }

    @classmethod
    def from_dict(
        cls, raw: dict[str, Any], *, source_run_id: str | None = None
    ) -> "MemoryChangeRequest":
        return cls(
            file=raw["file"],
            operation=MemoryOperation(raw["operation"]),
            content=raw.get("content", ""),
            section=raw.get("section", ""),
            field=raw.get("field", ""),
            source_run_id=source_run_id,
        )

    def summary(self) -> str:
        if self.operation is MemoryOperation.REPLACE_SECTION:
            return f"{self.operation.value} {self.file}#{self.section}"
        if self.operation is MemoryOperation.UPDATE_FIELD:
            return f"{self.operation.value} {self.file}#{self.field}"
        return f"{self.operation.value} {self.file}"
