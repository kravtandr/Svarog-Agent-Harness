"""Тесты chat (#22, §10.1): общая session, диалог между сообщениями."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.schema import AutonomyMode, PoliciesConfig, RuntimeConfig
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolDefinition,
    Usage,
)
from svarog_harness.policy.engine import PolicyEngine
from svarog_harness.runtime.loop import AgentLoop
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import Run
from svarog_harness.tools.registry import ToolRegistry
from svarog_harness.trace.recorder import TraceRecorder


class ScriptedProvider(ModelProvider):
    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)
        self.seen: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.seen.append(list(messages))
        return self.turns.pop(0)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


def _loop(provider: ModelProvider, db: AsyncSession, workspace: Path) -> AgentLoop:
    return AgentLoop(
        provider,
        ToolRegistry(),
        TraceRecorder(db),
        RuntimeConfig(),
        PolicyEngine(autonomy=AutonomyMode.YOLO, policies=PoliciesConfig(), workspace=workspace),
        workspace,
        model_name="test-model",
    )


async def test_runs_share_session_and_carry_history(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            CompletionResult(content="Привет, я Свар", usage=Usage(10, 5)),
            CompletionResult(content="Тебя зовут Аня", usage=Usage(10, 5)),
        ]
    )
    recorder = TraceRecorder(db)
    loop = _loop(provider, db, tmp_path)

    first = await loop.run("привет, меня зовут Аня", AutonomyMode.YOLO)
    run1 = await recorder.get_run(first.run_id)
    assert run1 is not None
    session_id = run1.session_id

    history = [
        ChatMessage(role="user", content="привет, меня зовут Аня"),
        ChatMessage(role="assistant", content=first.final_answer),
    ]
    second = await loop.run(
        "как меня зовут?", AutonomyMode.YOLO, session_id=session_id, history=history
    )
    run2 = await recorder.get_run(second.run_id)
    assert run2 is not None

    # Оба run'а — в одной session.
    assert run2.session_id == session_id
    runs = (await db.execute(select(Run))).scalars().all()
    assert len({r.session_id for r in runs}) == 1

    # Второй запрос к модели видел предыдущий диалог.
    second_request = provider.seen[-1]
    contents = [m.content for m in second_request]
    assert any("меня зовут Аня" in c for c in contents)
    assert any("Привет, я Свар" in c for c in contents)
