"""Интерфейс ModelProvider и модельно-независимые типы обмена (§3.10).

Runtime общается с LLM только через эти типы; форматы конкретных API
(OpenAI chat completions и т.п.) остаются внутри реализаций провайдеров.
"""

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolDefinition:
    """Описание tool для LLM: имя, назначение и JSON Schema аргументов."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCallRequest:
    """Запрошенный моделью вызов tool.

    Аргументы храним сырой JSON-строкой: модель может сгенерировать
    невалидный JSON, и решать, что с этим делать (вернуть ошибку в модель),
    должен agent loop, а не провайдер.
    """

    id: str
    name: str
    arguments_json: str

    def parse_arguments(self) -> dict[str, Any]:
        """Разобрать аргументы; ValueError — если это не JSON-объект."""
        try:
            parsed = json.loads(self.arguments_json) if self.arguments_json else {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"аргументы tool call не являются валидным JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"аргументы tool call должны быть JSON-объектом, получен {type(parsed).__name__}"
            )
        return parsed


@dataclass(frozen=True)
class ChatMessage:
    """Сообщение диалога в нейтральном формате.

    * assistant-сообщение может содержать tool_calls;
    * tool-сообщение обязано ссылаться на tool_call_id.
    """

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCallRequest, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class CompletionResult:
    """Один ход модели: текст и/или tool calls + учет токенов и стоимости."""

    content: str
    tool_calls: tuple[ToolCallRequest, ...] = ()
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    finish_reason: str | None = None


class ModelProvider(ABC):
    """Абстракция LLM-провайдера (единственная реализация в MVP — openai-compatible)."""

    @abstractmethod
    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        """Выполнить один запрос к модели (streaming внутри).

        `on_text_delta` вызывается на каждый фрагмент текстового ответа —
        для живого вывода в CLI; итоговый текст все равно возвращается целиком.
        """
