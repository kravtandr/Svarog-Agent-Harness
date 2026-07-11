"""ADR-0015 фаза 2: отложенная загрузка схем tools (progressive disclosure).

Deferred-tool зарегистрирован и исполним, но его полная JSON-схема не входит
в definitions(), пока модель не вызовет load_tool — до того в промпте только
строка «имя — однострочное назначение» внутри описания load_tool.
"""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.schema import AutonomyMode, PoliciesConfig, RuntimeConfig
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.policy.engine import PolicyEngine
from svarog_harness.runtime.checkpoint import LoopState
from svarog_harness.runtime.loop import AgentLoop
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import Checkpoint, RunState
from svarog_harness.tools.base import RiskLevel, Tool, ToolResult
from svarog_harness.tools.registry import LoadToolTool, ToolRegistry, UnknownToolError
from svarog_harness.trace.recorder import TraceRecorder


class _Args(BaseModel):
    text: str = Field(description="Что вернуть обратно")


class _CoreTool(Tool[_Args]):
    name = "core_echo"
    description = "Возвращает переданный текст"
    risk_level = RiskLevel.LOW
    args_model = _Args

    async def execute(self, args: _Args) -> ToolResult:
        return ToolResult.success(args.text)


class _DeferredTool(Tool[_Args]):
    name = "rare_gadget"
    description = "Редкий инструмент с тяжёлой схемой\n\nПодробности схемы на много строк."
    risk_level = RiskLevel.LOW
    args_model = _Args

    async def execute(self, args: _Args) -> ToolResult:
        return ToolResult.success(f"gadget: {args.text}")


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_CoreTool())
    registry.register(_DeferredTool(), deferred=True)
    return registry


# --- 2.1 ToolRegistry: core/deferred ------------------------------------------


def test_deferred_excluded_from_definitions_until_loaded() -> None:
    registry = _registry()
    assert [d.name for d in registry.definitions()] == ["core_echo"]
    # Зарегистрирован и доступен для исполнения: deferred — про экономию
    # промпта, не про права доступа (policy проверяется как обычно).
    assert registry.names() == ["core_echo", "rare_gadget"]
    assert registry.get("rare_gadget").name == "rare_gadget"


def test_deferred_summaries_are_first_description_line() -> None:
    registry = _registry()
    assert registry.deferred_summaries() == [("rare_gadget", "Редкий инструмент с тяжёлой схемой")]


def test_load_promotes_deferred_into_definitions() -> None:
    registry = _registry()
    assert registry.load("rare_gadget") is True
    assert [d.name for d in registry.definitions()] == ["core_echo", "rare_gadget"]
    assert registry.loaded_names() == ["rare_gadget"]
    assert registry.deferred_summaries() == []
    # Повторная загрузка идемпотентна.
    assert registry.load("rare_gadget") is False


def test_load_core_tool_is_noop() -> None:
    registry = _registry()
    assert registry.load("core_echo") is False
    assert registry.loaded_names() == []


def test_load_unknown_tool_raises() -> None:
    registry = _registry()
    with pytest.raises(UnknownToolError, match="неизвестный tool 'nope'"):
        registry.load("nope")


def test_restore_loaded_skips_vanished_names() -> None:
    """Resume после смены MCP-discovery: исчезнувшие имена не роняют restore."""
    registry = _registry()
    registry.restore_loaded(["rare_gadget", "vanished_tool"])
    assert registry.loaded_names() == ["rare_gadget"]


# --- 2.2 load_tool -------------------------------------------------------------


def test_load_tool_definition_lists_deferred_summaries() -> None:
    registry = _registry()
    tool = LoadToolTool(registry)
    description = tool.definition().description
    assert "rare_gadget — Редкий инструмент с тяжёлой схемой" in description
    registry.load("rare_gadget")
    assert "rare_gadget" not in tool.definition().description


async def test_load_tool_execute_promotes() -> None:
    registry = _registry()
    result = await LoadToolTool(registry).call({"name": "rare_gadget"})
    assert result.ok
    assert "со следующей итерации" in result.output
    assert [d.name for d in registry.definitions()] == ["core_echo", "rare_gadget"]


async def test_load_tool_already_available() -> None:
    registry = _registry()
    result = await LoadToolTool(registry).call({"name": "core_echo"})
    assert result.ok
    assert "уже доступен" in result.output


async def test_load_tool_unknown_lists_deferred() -> None:
    registry = _registry()
    result = await LoadToolTool(registry).call({"name": "nope"})
    assert not result.ok
    assert result.error is not None
    assert "rare_gadget" in result.error


def test_load_tool_metadata() -> None:
    tool = LoadToolTool(_registry())
    assert tool.risk_level is RiskLevel.LOW
    assert tool.action_type == "tool.load"
    # Мутирует только реестр (как read_skill — свой sink): параллелится безопасно.
    assert tool.is_read_only(tool.args_model(name="x")) is True


# --- 2.3 checkpoint/resume + loop end-to-end -----------------------------------


class _DefsCapturingProvider(ModelProvider):
    """Скриптованный провайдер, записывающий состав definitions по итерациям."""

    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)
        self.seen_tool_names: list[list[str]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.seen_tool_names.append([d.name for d in tools])
        return self.turns.pop(0)


def _tool_turn(*calls: ToolCallRequest) -> CompletionResult:
    return CompletionResult(
        content="", tool_calls=calls, usage=Usage(10, 5), finish_reason="tool_calls"
    )


def _final(text: str) -> CompletionResult:
    return CompletionResult(content=text, usage=Usage(10, 5), finish_reason="stop")


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


def _loop(
    provider: ModelProvider,
    db: AsyncSession,
    workspace: Path,
    registry: ToolRegistry,
    *,
    cfg: RuntimeConfig | None = None,
) -> AgentLoop:
    return AgentLoop(
        provider,
        registry,
        TraceRecorder(db),
        cfg or RuntimeConfig(),
        PolicyEngine(autonomy=AutonomyMode.YOLO, policies=PoliciesConfig(), workspace=workspace),
        workspace,
        model_name="test-model",
    )


def _registry_with_load_tool() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_CoreTool())
    registry.register(_DeferredTool(), deferred=True)
    registry.register(LoadToolTool(registry))
    return registry


def test_loop_state_roundtrip_keeps_loaded_tools(tmp_path: Path) -> None:
    state = LoopState(workspace=tmp_path, messages=[], loaded_tools=["rare_gadget"])
    restored = LoopState.from_dict(state.to_dict())
    assert restored.loaded_tools == ["rare_gadget"]
    # Старые checkpoint'ы без поля читаются с пустым списком.
    raw = state.to_dict()
    del raw["loaded_tools"]
    assert LoopState.from_dict(raw).loaded_tools == []


async def test_loop_defers_schema_until_load_tool(db: AsyncSession, tmp_path: Path) -> None:
    provider = _DefsCapturingProvider(
        [
            _tool_turn(
                ToolCallRequest(id="c1", name="load_tool", arguments_json='{"name": "rare_gadget"}')
            ),
            _tool_turn(
                ToolCallRequest(id="c2", name="rare_gadget", arguments_json='{"text": "го"}')
            ),
            _final("готово"),
        ]
    )
    registry = _registry_with_load_tool()
    outcome = await _loop(provider, db, tmp_path, registry).run("задача", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    # Итерация 1: схема deferred-tool скрыта, load_tool на месте.
    assert "rare_gadget" not in provider.seen_tool_names[0]
    assert "load_tool" in provider.seen_tool_names[0]
    # Итерация 2 (после load_tool): полная схема в definitions.
    assert "rare_gadget" in provider.seen_tool_names[1]

    # Checkpoint хранит множество загруженных имён (ADR-0005).
    checkpoints = (
        (await db.execute(select(Checkpoint).order_by(Checkpoint.created_at))).scalars().all()
    )
    assert checkpoints[-1].state["loaded_tools"] == ["rare_gadget"]


async def test_resume_restores_loaded_tools(db: AsyncSession, tmp_path: Path) -> None:
    """После resume реестр снова знает, какие deferred-схемы были загружены."""
    provider = _DefsCapturingProvider(
        [
            _tool_turn(
                ToolCallRequest(id="c1", name="load_tool", arguments_json='{"name": "rare_gadget"}')
            )
        ]
    )
    cfg = RuntimeConfig(max_iterations=1, refuel_after_iterations=5)
    outcome = await _loop(provider, db, tmp_path, _registry_with_load_tool(), cfg=cfg).run(
        "задача", AutonomyMode.YOLO
    )
    assert outcome.state is RunState.SUSPENDED

    recorder = TraceRecorder(db)
    run, raw_state = await recorder.load_resumable(outcome.run_id[:8])
    state = LoopState.from_dict(raw_state)

    # Свежий процесс: новый реестр ничего не знает о прошлой загрузке.
    resumed_provider = _DefsCapturingProvider([_final("после resume")])
    resumed = _loop(
        resumed_provider,
        db,
        tmp_path,
        _registry_with_load_tool(),
        cfg=RuntimeConfig(max_iterations=10, refuel_after_iterations=5),
    )
    result = await resumed.resume(run, state)
    assert result.state is RunState.COMPLETED
    # Первая же итерация после resume видит полную схему rare_gadget.
    assert "rare_gadget" in resumed_provider.seen_tool_names[0]
