"""Тесты checkpoint/resume (ADR-0005): write-ahead, suspend, recovery."""

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
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.policy.engine import PolicyEngine
from svarog_harness.runtime.checkpoint import LoopState
from svarog_harness.runtime.loop import AgentLoop
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import Checkpoint, Message, Run, RunState
from svarog_harness.tools.file_tools import file_tools
from svarog_harness.tools.registry import ToolRegistry
from svarog_harness.trace.lookup import RunNotResumableError
from svarog_harness.trace.recorder import TraceRecorder


class ScriptedProvider(ModelProvider):
    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        return self.turns.pop(0)


def _final(text: str) -> CompletionResult:
    return CompletionResult(content=text, usage=Usage(10, 5), finish_reason="stop")


def _tool_turn(*calls: ToolCallRequest) -> CompletionResult:
    return CompletionResult(
        content="", tool_calls=calls, usage=Usage(10, 5), finish_reason="tool_calls"
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
    provider: ModelProvider,
    db: AsyncSession,
    workspace: Path,
    *,
    cfg: RuntimeConfig | None = None,
) -> AgentLoop:
    registry = ToolRegistry()
    for tool in file_tools(workspace):
        registry.register(tool)
    return AgentLoop(
        provider,
        registry,
        TraceRecorder(db),
        cfg or RuntimeConfig(),
        PolicyEngine(autonomy=AutonomyMode.YOLO, policies=PoliciesConfig(), workspace=workspace),
        workspace,
        model_name="test-model",
    )


async def test_checkpoints_written_during_run(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            _tool_turn(ToolCallRequest(id="c1", name="list_dir", arguments_json="{}")),
            _final("готово"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    checkpoints = (
        (await db.execute(select(Checkpoint).order_by(Checkpoint.created_at))).scalars().all()
    )
    # Стартовый + write-ahead (pending) + после исполнения tool call.
    assert len(checkpoints) == 3
    assert checkpoints[0].state["pending_tool_calls"] == []
    assert checkpoints[1].state["pending_tool_calls"][0]["name"] == "list_dir"
    assert checkpoints[2].state["pending_tool_calls"] == []
    assert checkpoints[2].state["workspace"] == str(tmp_path)


async def test_suspended_run_resumes_and_completes(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            _tool_turn(ToolCallRequest(id="c1", name="list_dir", arguments_json="{}")),
            _tool_turn(ToolCallRequest(id="c2", name="list_dir", arguments_json="{}")),
            _final("после resume"),
        ]
    )
    cfg = RuntimeConfig(max_iterations=2, refuel_after_iterations=1)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("длинная задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.SUSPENDED
    assert outcome.iterations == 2

    # Пользователь поднял лимит — resume продолжает с места остановки.
    recorder = TraceRecorder(db)
    run, raw_state = await recorder.load_resumable(outcome.run_id[:8])
    state = LoopState.from_dict(raw_state)
    assert state.iterations == 2

    resumed = _loop(
        provider, db, tmp_path, cfg=RuntimeConfig(max_iterations=10, refuel_after_iterations=5)
    )
    result = await resumed.resume(run, state)
    assert result.state is RunState.COMPLETED
    assert result.final_answer == "после resume"
    assert result.iterations == 3
    # Токены накоплены за все итерации, включая до приостановки.
    assert result.tokens_used == 45

    # Нумерация сообщений продолжилась без конфликтов UniqueConstraint.
    messages = (await db.execute(select(Message).order_by(Message.index_in_run))).scalars().all()
    assert [m.index_in_run for m in messages] == list(range(len(messages)))
    assert messages[-1].content["content"] == "после resume"


async def test_resume_executes_pending_write_ahead_calls(db: AsyncSession, tmp_path: Path) -> None:
    """Вызов, зафиксированный в checkpoint до исполнения, доисполняется при resume."""
    provider = ScriptedProvider([_final("файл записан")])
    recorder = TraceRecorder(db)
    run = await recorder.start_run(task="задача", autonomy="yolo", model="test-model")
    pending = ToolCallRequest(
        id="c1",
        name="write_file",
        arguments_json='{"path": "wal.txt", "content": "из write-ahead"}',
    )
    state = LoopState(
        workspace=tmp_path,
        messages=[
            ChatMessage(role="user", content="задача"),
            ChatMessage(role="assistant", content="", tool_calls=(pending,)),
        ],
        iterations=1,
        pending_tool_calls=(pending,),
    )
    await recorder.save_checkpoint(run, iteration=1, state=state.to_dict())
    await recorder.set_run_state(run, RunState.SUSPENDED, error="искусственная остановка")

    loaded_run, raw_state = await TraceRecorder(db).load_resumable(run.id)
    outcome = await _loop(provider, db, tmp_path).resume(loaded_run, LoopState.from_dict(raw_state))
    assert outcome.state is RunState.COMPLETED
    assert (tmp_path / "wal.txt").read_text(encoding="utf-8") == "из write-ahead"


async def test_recover_interrupted_runs(db: AsyncSession) -> None:
    recorder = TraceRecorder(db)
    run = await recorder.start_run(task="упавший", autonomy="yolo", model="m")
    assert run.state is RunState.RUNNING

    recovered = await recorder.recover_interrupted_runs()
    assert [r.id for r in recovered] == [run.id]
    stored = (await db.execute(select(Run))).scalar_one()
    assert stored.state is RunState.SUSPENDED
    assert stored.error is not None
    assert "прерван" in stored.error

    # Повторный вызов — no-op.
    assert await recorder.recover_interrupted_runs() == []


async def test_load_resumable_rejects_completed_run(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider([_final("готово")])
    outcome = await _loop(provider, db, tmp_path).run("задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    with pytest.raises(RunNotResumableError, match="completed"):
        await TraceRecorder(db).load_resumable(outcome.run_id)


async def test_load_resumable_requires_checkpoint(db: AsyncSession) -> None:
    recorder = TraceRecorder(db)
    run = await recorder.start_run(task="без checkpoint", autonomy="yolo", model="m")
    await recorder.set_run_state(run, RunState.SUSPENDED)

    with pytest.raises(RunNotResumableError, match="нет checkpoint"):
        await recorder.load_resumable(run.id)
