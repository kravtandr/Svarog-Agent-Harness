"""Реестр tools: регистрация и генерация tool definitions для LLM (§6.5)."""

from typing import Any

from svarog_harness.llm.provider import ToolDefinition
from svarog_harness.tools.base import Tool


class UnknownToolError(Exception):
    def __init__(self, name: str, known: list[str]) -> None:
        super().__init__(f"неизвестный tool '{name}' (доступны: {', '.join(known) or 'нет'})")
        self.name = name


class ToolRegistry:
    # Tool[Any]: Generic инвариантен, реестр хранит tools с разными args-моделями.
    def __init__(self) -> None:
        self._tools: dict[str, Tool[Any]] = {}

    def register(self, tool: Tool[Any]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' уже зарегистрирован")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool[Any]:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(name, self.names()) from None

    def names(self) -> list[str]:
        return sorted(self._tools)

    def definitions(self) -> list[ToolDefinition]:
        return [self._tools[name].definition() for name in self.names()]
