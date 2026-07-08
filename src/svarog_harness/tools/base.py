"""Базовый класс Tool (§6.5).

Каждый tool декларирует метаданные для Policy Engine (risk_level,
sandbox_requirement) и pydantic-модель аргументов, из которой генерируется
JSON Schema для LLM. Валидация аргументов и timeout применяются в `call()`
одинаково для всех tools; реализация пишет только `execute()`.
"""

import asyncio
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from svarog_harness.llm.provider import ToolDefinition


class RiskLevel(StrEnum):
    """Уровни риска действий (§3.6)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SandboxRequirement(StrEnum):
    """Требование tool к среде исполнения (ADR-0002).

    NONE — работает на хосте (файловые операции внутри workspace);
    REQUIRED — только в sandbox (произвольное исполнение кода). В M1
    доступен единственный backend local-trusted, требование записывается
    в метаданные и начинает enforced'иться с появлением sandbox в M2.
    """

    NONE = "none"
    REQUIRED = "required"


class ToolError(Exception):
    """Ожидаемая ошибка исполнения tool — превращается в error-результат для модели."""


def truncate_text(text: str, limit: int) -> str:
    """Обрезать вывод tool, чтобы не раздувать контекст (§6.3 backpressure)."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n… [вывод обрезан: {len(text)} символов, лимит {limit}]"


class ToolResult(BaseModel):
    ok: bool
    # Текст для модели: содержимое файла, stdout, сообщение об успехе.
    output: str = ""
    error: str | None = None

    @classmethod
    def success(cls, output: str) -> "ToolResult":
        return cls(ok=True, output=output)

    @classmethod
    def failure(cls, error: str) -> "ToolResult":
        return cls(ok=False, error=error)


class Tool[ArgsT: BaseModel](ABC):
    # Не ClassVar: статические tools задают их на уровне класса, а динамические
    # (MCP — имя/риск/схема известны только при discovery) — на инстансе.
    name: str
    description: str
    risk_level: RiskLevel
    sandbox_requirement: SandboxRequirement = SandboxRequirement.NONE
    # Типизированная операция для Policy Engine и правил policies/*.yaml
    # (например "file.write"); None — используется имя tool.
    action_type: str | None = None
    # Tool может переопределить timeout на инстансе (bash берет его из конфига).
    timeout_sec: float = 60.0

    # Параметризованный тип нельзя объявить ClassVar — задается в подклассах на уровне класса.
    args_model: type[ArgsT]

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.args_model.model_json_schema(),
        )

    @abstractmethod
    async def execute(self, args: ArgsT) -> ToolResult:
        """Выполнить tool с уже валидированными аргументами."""

    async def call(self, arguments: dict[str, Any]) -> ToolResult:
        """Валидация аргументов → execute с timeout; все ошибки — в ToolResult."""
        try:
            args = self.args_model.model_validate(arguments)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(loc) for loc in err['loc']) or '<root>'}: {err['msg']}"
                for err in exc.errors()
            )
            return ToolResult.failure(f"невалидные аргументы {self.name}: {problems}")
        try:
            return await asyncio.wait_for(self.execute(args), timeout=self.timeout_sec)
        except TimeoutError:
            return ToolResult.failure(f"{self.name} превысил timeout {self.timeout_sec}s")
        except ToolError as exc:
            return ToolResult.failure(str(exc))
