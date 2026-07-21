"""Реестр tools: регистрация и генерация tool definitions для LLM (§6.5).

ADR-0015 фаза 2 — progressive disclosure для tool-схем: deferred-tool
зарегистрирован и исполним, но его полная JSON-схема не входит в
`definitions()`, пока не вызван `load_tool`. До того модель видит только
строку «имя — однострочное назначение» в описании `load_tool` —
provider-neutral аналог ToolSearch, без beta API.
"""

import difflib
import json
from typing import Any

from pydantic import BaseModel, Field

from svarog_harness.llm.provider import ToolCallRequest, ToolDefinition
from svarog_harness.tools.base import RiskLevel, Tool, ToolResult


class UnknownToolError(Exception):
    def __init__(self, name: str, known: list[str]) -> None:
        self.name = name
        self.suggestion = _closest_name(name, known)
        if self.suggestion is not None:
            message = f"неизвестный tool '{name}'; возможно, имелся в виду '{self.suggestion}'"
        else:
            message = f"неизвестный tool '{name}' (доступны: {', '.join(known) or 'нет'})"
        super().__init__(message)


def _normalized(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "")


def _closest_name(name: str, known: list[str]) -> str | None:
    """Ближайшее имя инструмента — только подсказка, никогда не исполнение."""
    normalized = {_normalized(candidate): candidate for candidate in known}
    exact = normalized.get(_normalized(name))
    if exact is not None:
        return exact
    matches = difflib.get_close_matches(_normalized(name), list(normalized), n=1, cutoff=0.7)
    return normalized[matches[0]] if matches else None


class ToolRegistry:
    # Tool[Any]: Generic инвариантен, реестр хранит tools с разными args-моделями.
    def __init__(self) -> None:
        self._tools: dict[str, Tool[Any]] = {}
        # Ось промпта, не доступа: deferred-tool исполним через get(), но его
        # схема скрыта из definitions(), пока имя не попадёт в _loaded.
        self._deferred: set[str] = set()
        self._loaded: set[str] = set()
        self._external: set[str] = set()

    def register(self, tool: Tool[Any], *, deferred: bool = False, external: bool = False) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' уже зарегистрирован")
        self._tools[tool.name] = tool
        if deferred:
            self._deferred.add(tool.name)
        if external:
            self._external.add(tool.name)

    def get(self, name: str) -> Tool[Any]:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(name, self.names()) from None

    def names(self) -> list[str]:
        return sorted(self._tools)

    def prepare_arguments(
        self, tool: Tool[Any], call: ToolCallRequest
    ) -> tuple[dict[str, Any], list[str]]:
        """Разобрать аргументы вызова, починив известные дефекты формы.

        Строгий разбор делегируется `call.parse_arguments()`. Если он падает,
        пробуем починить только двойную сериализацию (модель обернула JSON
        ещё раз в строку) — если и это не JSON-объект, исходная ошибка
        пробрасывается без изменений. После успешного разбора (строгого или
        починенного) разворачиваем обёртку `{"arguments": {...}}`, но только
        если у tool'а нет своего параметра `arguments` и это единственный
        ключ — иначе разворачивание исказило бы реальный смысл вызова.
        Список выполненных ремонтов уходит в trace — молчаливая нормализация
        ломала бы свойство «trace отвечает, что именно исполнялось».
        """
        repairs: list[str] = []
        try:
            parsed = call.parse_arguments()
        except ValueError:
            repaired = self._repair_double_encoded(call.arguments_json)
            if repaired is None:
                raise
            parsed = repaired
            repairs.append("double_encoded")

        envelope = parsed.get("arguments")
        if (
            list(parsed) == ["arguments"]
            and isinstance(envelope, dict)
            and "arguments" not in tool.args_model.model_fields
        ):
            parsed = envelope
            repairs.append("unwrapped")

        return parsed, repairs

    @staticmethod
    def _repair_double_encoded(raw: str) -> dict[str, Any] | None:
        """Разобрать дважды сериализованный JSON: строка внутри строки."""
        try:
            outer = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return None
        if not isinstance(outer, str):
            return None
        try:
            inner = json.loads(outer)
        except json.JSONDecodeError:
            return None
        return inner if isinstance(inner, dict) else None

    def definitions(self) -> list[ToolDefinition]:
        """Схемы для промпта: встроенные → внешние → load_tool.

        Порядок стабилизирует префикс промпта (блок A §3): появление и
        загрузка MCP-инструментов дописываются в хвост и не сдвигают
        встроенную часть. load_tool идёт последним намеренно — его
        description содержит сводку незагруженных deferred-схем и меняется
        при каждой загрузке.
        """
        visible = [
            name for name in self.names() if name not in self._deferred or name in self._loaded
        ]
        builtin = [n for n in visible if n not in self._external and n != _LOAD_TOOL_NAME]
        external = [n for n in visible if n in self._external and n != _LOAD_TOOL_NAME]
        trailing = [n for n in visible if n == _LOAD_TOOL_NAME]
        return [self._tools[name].definition() for name in builtin + external + trailing]

    def deferred_summaries(self) -> list[tuple[str, str]]:
        """(имя, первая строка description) незагруженных deferred-tools."""
        return [
            (name, self._tools[name].description.splitlines()[0])
            for name in sorted(self._deferred - self._loaded)
        ]

    def load(self, name: str) -> bool:
        """Перевести deferred-tool в загруженные; True — если состав definitions вырос."""
        if name not in self._tools:
            raise UnknownToolError(name, self.names())
        if name not in self._deferred or name in self._loaded:
            return False
        self._loaded.add(name)
        return True

    def loaded_names(self) -> list[str]:
        return sorted(self._loaded)

    def restore_loaded(self, names: list[str]) -> None:
        """Восстановить загруженные после resume (ADR-0005 checkpoint).

        Исчезнувшие имена молча пропускаются: MCP-discovery между
        checkpoint'ом и resume мог измениться, и это не повод ронять run.
        """
        for name in names:
            if name in self._deferred:
                self._loaded.add(name)


class LoadToolArgs(BaseModel):
    name: str = Field(description="Имя tool, чью полную схему нужно загрузить")


class LoadToolTool(Tool[LoadToolArgs]):
    name = "load_tool"
    action_type = "tool.load"
    risk_level = RiskLevel.LOW
    args_model = LoadToolArgs

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def is_read_only(self, args: LoadToolArgs) -> bool:
        # Мутирует только состав промпта (реестр), не workspace — как read_skill.
        return True

    @property
    def description(self) -> str:  # type: ignore[override]
        summaries = self._registry.deferred_summaries()
        if not summaries:
            return "Загрузить полную схему отложенного tool (все уже загружены)"
        lines = "\n".join(f"- {name} — {summary}" for name, summary in summaries)
        return (
            "Загрузить полную схему отложенного tool; со следующей итерации "
            f"он станет доступен для вызова. Отложенные tools:\n{lines}"
        )

    async def execute(self, args: LoadToolArgs) -> ToolResult:
        try:
            promoted = self._registry.load(args.name)
        except UnknownToolError:
            deferred = ", ".join(name for name, _ in self._registry.deferred_summaries())
            return ToolResult.failure(
                f"tool '{args.name}' не найден (отложенные: {deferred or 'нет'})"
            )
        if not promoted:
            return ToolResult.success(f"tool '{args.name}' уже доступен — загрузка не требуется")
        return ToolResult.success(
            f"схема tool '{args.name}' загружена; вызов доступен со следующей итерации"
        )


# Берём значение с атрибута класса, а не дублируем строковый литерал: имя
# load_tool используется в definitions() для сортировки его в самый конец.
_LOAD_TOOL_NAME = LoadToolTool.name
