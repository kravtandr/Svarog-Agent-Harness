"""Тесты Tool base и ToolRegistry: валидация, timeout, definitions."""

import asyncio

import pytest
from pydantic import BaseModel, Field

from svarog_harness.tools.base import RiskLevel, Tool, ToolError, ToolResult
from svarog_harness.tools.registry import ToolRegistry, UnknownToolError


class EchoArgs(BaseModel):
    text: str = Field(description="Что вернуть обратно")


class EchoTool(Tool[EchoArgs]):
    name = "echo"
    description = "Возвращает переданный текст"
    risk_level = RiskLevel.LOW
    args_model = EchoArgs

    async def execute(self, args: EchoArgs) -> ToolResult:
        return ToolResult.success(args.text)


class SlowTool(Tool[EchoArgs]):
    name = "slow"
    description = "Спит дольше своего timeout"
    risk_level = RiskLevel.LOW
    timeout_sec = 0.05
    args_model = EchoArgs

    async def execute(self, args: EchoArgs) -> ToolResult:
        await asyncio.sleep(1)
        return ToolResult.success("не должно случиться")


class FailingTool(Tool[EchoArgs]):
    name = "failing"
    description = "Бросает ToolError"
    risk_level = RiskLevel.LOW
    args_model = EchoArgs

    async def execute(self, args: EchoArgs) -> ToolResult:
        raise ToolError("ожидаемая ошибка")


async def test_call_validates_and_executes() -> None:
    result = await EchoTool().call({"text": "привет"})
    assert result.ok
    assert result.output == "привет"


async def test_call_rejects_invalid_arguments() -> None:
    result = await EchoTool().call({"wrong": 1})
    assert not result.ok
    assert result.error is not None
    assert "невалидные аргументы echo" in result.error
    assert "text" in result.error


async def test_call_enforces_timeout() -> None:
    result = await SlowTool().call({"text": "x"})
    assert not result.ok
    assert result.error is not None
    assert "timeout" in result.error


async def test_tool_error_becomes_failure_result() -> None:
    result = await FailingTool().call({"text": "x"})
    assert not result.ok
    assert result.error == "ожидаемая ошибка"


def test_definition_exposes_json_schema() -> None:
    definition = EchoTool().definition()
    assert definition.name == "echo"
    assert definition.input_schema["properties"]["text"]["type"] == "string"
    assert "text" in definition.input_schema["required"]


def test_registry_register_get_definitions() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(SlowTool())
    assert registry.names() == ["echo", "slow"]
    assert registry.get("echo").name == "echo"
    assert [d.name for d in registry.definitions()] == ["echo", "slow"]


def test_registry_rejects_duplicates() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    with pytest.raises(ValueError, match="уже зарегистрирован"):
        registry.register(EchoTool())


def test_registry_unknown_tool() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    with pytest.raises(UnknownToolError, match="неизвестный tool 'nope'"):
        registry.get("nope")


def test_execution_metadata_fail_closed_defaults() -> None:
    """ADR-0015 §1.1: не знаешь про tool — считай, что он пишет и не параллелится."""
    tool = EchoTool()
    args = EchoArgs(text="x")
    assert tool.is_read_only(args) is False
    assert tool.is_concurrency_safe(args) is False


def test_concurrency_safe_follows_read_only_override() -> None:
    class ReadingTool(EchoTool):
        def is_read_only(self, args: EchoArgs) -> bool:
            return True

    tool = ReadingTool()
    args = EchoArgs(text="x")
    assert tool.is_read_only(args) is True
    assert tool.is_concurrency_safe(args) is True
