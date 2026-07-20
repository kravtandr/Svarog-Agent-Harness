"""Тесты Tool base и ToolRegistry: валидация, timeout, definitions."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from svarog_harness.llm.provider import ToolCallRequest
from svarog_harness.tools.base import RiskLevel, Tool, ToolError, ToolResult
from svarog_harness.tools.file_tools import ReadFileTool
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


def tmp_workspace() -> Path:
    """Одноразовый workspace для file-tools в тестах реестра.

    Конструктору ReadFileTool достаточно валидного Path — он не трогает
    диск, пока не вызван execute(), поэтому фикстура pytest не нужна.
    """
    return Path("workspace")


class _ArgsWithArgumentsField(BaseModel):
    arguments: str = Field(description="Собственный параметр arguments у tool'а")


class _ToolWithArgumentsParam(Tool[_ArgsWithArgumentsField]):
    name = "custom_with_arguments"
    description = "Tool с собственным параметром arguments — конверт не разворачивается"
    risk_level = RiskLevel.LOW
    args_model = _ArgsWithArgumentsField

    async def execute(self, args: _ArgsWithArgumentsField) -> ToolResult:
        return ToolResult.success(args.arguments)


def _tool_with_arguments_param() -> Tool[Any]:
    return _ToolWithArgumentsParam()


def test_prepare_arguments_unwraps_double_encoded_json() -> None:
    """Модель сериализовала аргументы дважды — распаковываем (блок A §4)."""
    registry = ToolRegistry()
    tool = ReadFileTool(tmp_workspace())
    registry.register(tool)
    call = ToolCallRequest(id="c1", name="read_file", arguments_json='"{\\"path\\": \\"a.txt\\"}"')

    arguments, repairs = registry.prepare_arguments(tool, call)

    assert arguments == {"path": "a.txt"}
    assert repairs == ["double_encoded"]


def test_prepare_arguments_unwraps_arguments_envelope() -> None:
    """Обёртка {"arguments": {...}} разворачивается, если у tool'а нет
    собственного параметра arguments."""
    registry = ToolRegistry()
    tool = ReadFileTool(tmp_workspace())
    registry.register(tool)
    call = ToolCallRequest(
        id="c1", name="read_file", arguments_json='{"arguments": {"path": "a.txt"}}'
    )

    arguments, repairs = registry.prepare_arguments(tool, call)

    assert arguments == {"path": "a.txt"}
    assert repairs == ["unwrapped"]


def test_prepare_arguments_keeps_own_arguments_parameter() -> None:
    """У tool'а есть собственный параметр arguments → обёртка НЕ разворачивается."""
    registry = ToolRegistry()
    tool = _tool_with_arguments_param()
    registry.register(tool)
    call = ToolCallRequest(
        id="c1", name="custom_with_arguments", arguments_json='{"arguments": {"path": "a.txt"}}'
    )

    arguments, repairs = registry.prepare_arguments(tool, call)

    assert arguments == {"arguments": {"path": "a.txt"}}
    assert repairs == []


def test_prepare_arguments_passes_clean_input_through() -> None:
    registry = ToolRegistry()
    tool = ReadFileTool(tmp_workspace())
    registry.register(tool)
    call = ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "a.txt"}')

    arguments, repairs = registry.prepare_arguments(tool, call)

    assert arguments == {"path": "a.txt"}
    assert repairs == []


def test_prepare_arguments_rejects_invalid_json() -> None:
    registry = ToolRegistry()
    tool = ReadFileTool(tmp_workspace())
    registry.register(tool)
    call = ToolCallRequest(id="c1", name="read_file", arguments_json="{не json")

    with pytest.raises(ValueError, match="JSON"):
        registry.prepare_arguments(tool, call)


def test_unknown_tool_error_suggests_close_name() -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool(tmp_workspace()))

    with pytest.raises(UnknownToolError) as excinfo:
        registry.get("readfile")

    assert excinfo.value.suggestion == "read_file"
    assert "read_file" in str(excinfo.value)


def test_unknown_tool_error_lists_tools_when_no_close_name() -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool(tmp_workspace()))

    with pytest.raises(UnknownToolError) as excinfo:
        registry.get("совершенно_другое")

    assert excinfo.value.suggestion is None
    assert "доступны" in str(excinfo.value)
