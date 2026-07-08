"""Тесты refuel (§6.10, §20): task_state.md, сброс и пересборка контекста inline."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
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
from svarog_harness.runtime.loop import AgentLoop
from svarog_harness.runtime.refuel import build_task_state
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import RunState
from svarog_harness.tools.file_tools import file_tools
from svarog_harness.tools.registry import ToolRegistry
from svarog_harness.trace.recorder import TraceRecorder


def test_build_task_state_has_sections() -> None:
    messages = [
        ChatMessage(role="user", content="задача"),
        ChatMessage(
            role="assistant",
            content="нашёл важное",
            tool_calls=(ToolCallRequest(id="c1", name="list_dir", arguments_json="{}"),),
        ),
    ]
    text = build_task_state("почини баг", messages, iterations=5)
    assert "# Task state" in text
    assert "почини баг" in text
    assert "list_dir" in text
    assert "нашёл важное" in text
    assert "Выполнено итераций: 5" in text


class ScriptedProvider(ModelProvider):
    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)
        self.seen_messages: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.seen_messages.append(list(messages))
        return self.turns.pop(0)


def _tool_turn(i: int) -> CompletionResult:
    return CompletionResult(
        content=f"шаг {i}",
        tool_calls=(ToolCallRequest(id=f"c{i}", name="list_dir", arguments_json="{}"),),
        usage=Usage(10, 5),
        finish_reason="tool_calls",
    )


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
    provider: ModelProvider, db: AsyncSession, workspace: Path, cfg: RuntimeConfig
) -> AgentLoop:
    registry = ToolRegistry()
    for tool in file_tools(workspace):
        registry.register(tool)
    return AgentLoop(
        provider,
        registry,
        TraceRecorder(db),
        cfg,
        PolicyEngine(autonomy=AutonomyMode.YOLO, policies=PoliciesConfig(), workspace=workspace),
        workspace,
        model_name="test-model",
    )


async def test_refuel_writes_task_state_and_rebuilds_context(
    db: AsyncSession, tmp_path: Path
) -> None:
    # refuel на 2-й итерации, всего разрешено 6.
    cfg = RuntimeConfig(max_iterations=6, refuel_after_iterations=2)
    provider = ScriptedProvider(
        [
            _tool_turn(0),
            _tool_turn(1),
            _tool_turn(2),
            CompletionResult(content="готово", usage=Usage(10, 5)),
        ]
    )
    outcome = await _loop(provider, db, tmp_path, cfg).run("длинная задача", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    # task_state.md записан на диск при refuel.
    assert (tmp_path / "task_state.md").exists()
    task_state = (tmp_path / "task_state.md").read_text(encoding="utf-8")
    assert "# Task state" in task_state

    # После refuel контекст пересобран: 3-й запрос к модели короткий (system+user),
    # а не накопленная история из 6+ сообщений.
    request_after_refuel = provider.seen_messages[2]
    assert len(request_after_refuel) == 2
    assert request_after_refuel[0].role == "system"
    assert "task_state" in request_after_refuel[1].content.lower()


async def test_max_iterations_still_caps_across_refuel(db: AsyncSession, tmp_path: Path) -> None:
    # refuel каждые 2 итерации, но всего не больше 3 — max должен сработать.
    cfg = RuntimeConfig(max_iterations=3, refuel_after_iterations=2)
    provider = ScriptedProvider([_tool_turn(i) for i in range(10)])
    outcome = await _loop(provider, db, tmp_path, cfg).run("бесконечная", AutonomyMode.YOLO)

    assert outcome.state is RunState.SUSPENDED
    assert outcome.iterations == 3
    assert outcome.error is not None
    assert "лимит итераций" in outcome.error
