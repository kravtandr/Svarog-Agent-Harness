"""Модели MCP-интеграции: спецификация tool и абстракция backend (§9)."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from svarog_harness.tools.base import RiskLevel


class MCPError(Exception):
    """Ошибка связи с MCP-сервером или вызова MCP tool."""


@dataclass(frozen=True)
class MCPToolSpec:
    """Инструмент, обнаруженный на MCP-сервере (discovery)."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})


class MCPBackend(ABC):
    """Подключение к одному MCP-серверу: discovery + вызов tools.

    server_name — префикс в имени/action_type tool'а; risk — риск по умолчанию
    для всех его инструментов (§9).
    """

    server_name: str
    risk: RiskLevel

    @abstractmethod
    def specs(self) -> list[MCPToolSpec]:
        """Обнаруженные инструменты сервера (заполняются при connect)."""

    @abstractmethod
    async def call(self, tool: str, arguments: dict[str, Any]) -> str:
        """Вызвать MCP tool и вернуть текстовый результат."""

    async def close(self) -> None:  # noqa: B027 - осознанный необязательный no-op хук
        """Закрыть соединение; по умолчанию нечего закрывать (fake-backend'ы)."""
