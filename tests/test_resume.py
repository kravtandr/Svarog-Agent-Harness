"""Тесты checkpoint/resume (ADR-0005): write-ahead, suspend, recovery."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from pydantic import BaseModel
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
from svarog_harness.storage.models import Approval, Checkpoint, Message, Run, RunState
from svarog_harness.tools.base import RiskLevel, Tool, ToolResult
from svarog_harness.tools.file_tools import file_tools
from svarog_harness.tools.registry import ToolRegistry
from svarog_harness.trace.lookup import RunNotResumableError
from svarog_harness.trace.recorder import TraceRecorder, WorkspaceBusyError


class _NoArgs(BaseModel):
    pass


class _HighRiskTool(Tool[_NoArgs]):
    """Tool для approval-сценария (блок A §1, регрессия на resume-после-approval)."""

    name = "high_risk_action"
    action_type = "test.high_risk"
    description = "тестовый high-risk tool, требующий approval в supervised"
    risk_level = RiskLevel.HIGH
    args_model = _NoArgs

    async def execute(self, args: _NoArgs) -> ToolResult:
        return ToolResult.success("выполнено")


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
    # refuel отключён (порог > max), чтобы проверить именно стоп-кран max_iterations.
    cfg = RuntimeConfig(max_iterations=2, refuel_after_iterations=5)
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
    # Накопитель фаз (блок A §5) тоже учитывает вызовы модели и до
    # приостановки, и после — центральное требование к resume.
    assert run.meta["phases"]["llm_call"]["count"] == 3

    # Нумерация сообщений продолжилась без конфликтов UniqueConstraint.
    messages = (await db.execute(select(Message).order_by(Message.index_in_run))).scalars().all()
    assert [m.index_in_run for m in messages] == list(range(len(messages)))
    assert messages[-1].content["content"] == "после resume"


async def test_resume_survives_malformed_phase_meta(db: AsyncSession, tmp_path: Path) -> None:
    """Run.meta["phases"] со строкой/числом вместо словаря не должен ронять
    resume() исключением до перевода run в failed — таймер просто игнорирует
    испорченный агрегат и продолжает с нуля."""
    provider = ScriptedProvider([_final("после resume")])
    recorder = TraceRecorder(db)
    run = await recorder.start_run(task="задача", autonomy="yolo", model="test-model")
    state = LoopState(
        workspace=tmp_path,
        messages=[
            ChatMessage(role="system", content="ты агент"),
            ChatMessage(role="user", content="задача"),
        ],
        iterations=1,
    )
    await recorder.save_checkpoint(run, iteration=1, state=state.to_dict())
    await recorder.set_run_state(run, RunState.SUSPENDED, error="искусственная остановка")
    # Испорченный агрегат фаз — например, ручная правка БД или битая миграция.
    await recorder.merge_run_meta(run, {"phases": "мусор"})

    loaded_run, raw_state = await TraceRecorder(db).load_resumable(run.id)
    outcome = await _loop(provider, db, tmp_path).resume(loaded_run, LoopState.from_dict(raw_state))
    assert outcome.state is RunState.COMPLETED


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
            ChatMessage(role="system", content="ты агент"),
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
    from datetime import timedelta

    from svarog_harness.storage.models import utcnow

    recorder = TraceRecorder(db)
    run = await recorder.start_run(task="упавший", autonomy="yolo", model="m")
    assert run.state is RunState.RUNNING
    # Симулируем мёртвый процесс: heartbeat протух (ADR-0015 §0.5).
    run.heartbeat_at = utcnow() - timedelta(seconds=3600)
    await db.commit()

    recovered = await recorder.recover_interrupted_runs()
    assert [r.id for r in recovered] == [run.id]
    stored = (await db.execute(select(Run))).scalar_one()
    assert stored.state is RunState.SUSPENDED
    assert stored.error is not None
    assert "прерван" in stored.error

    # Повторный вызов — no-op.
    assert await recorder.recover_interrupted_runs() == []


async def test_recover_leaves_live_run_alone(db: AsyncSession) -> None:
    """Живой run в другом процессе (свежий heartbeat) не приостанавливается
    ложно (ADR-0015 §0.5) — снят прежний компромисс recovery."""
    recorder = TraceRecorder(db)
    run = await recorder.start_run(task="живой", autonomy="yolo", model="m")
    assert run.state is RunState.RUNNING  # heartbeat свежий из start_run

    recovered = await recorder.recover_interrupted_runs()
    assert recovered == []
    stored = (await db.execute(select(Run))).scalar_one()
    assert stored.state is RunState.RUNNING


async def test_workspace_lease_blocks_second_run(db: AsyncSession) -> None:
    """Второй run на залоченном workspace отклоняется, пока первый жив (§0.5)."""
    recorder = TraceRecorder(db)
    await recorder.start_run(task="первый", autonomy="yolo", model="m", workspace="/ws/a")

    with pytest.raises(WorkspaceBusyError):
        await recorder.acquire_workspace_lease("/ws/a")
    # Другой workspace свободен.
    await recorder.acquire_workspace_lease("/ws/b")


async def test_workspace_lease_free_after_stale_heartbeat(db: AsyncSession) -> None:
    from datetime import timedelta

    from svarog_harness.storage.models import utcnow

    recorder = TraceRecorder(db)
    run = await recorder.start_run(task="упал", autonomy="yolo", model="m", workspace="/ws/a")
    run.heartbeat_at = utcnow() - timedelta(seconds=3600)
    await db.commit()
    # Мёртвый держатель lease не блокирует новый run.
    await recorder.acquire_workspace_lease("/ws/a")


async def test_merge_run_meta_preserves_concurrent_cancel_flag(tmp_path: Path) -> None:
    """merge_run_meta не должен затирать флаг, выставленный параллельно
    другой сессией БД, устаревшей локальной копией run.meta.

    Две независимые AsyncSession на одном engine имитируют реальный сценарий:
    loop держит run в памяти своей сессии, а gateway ставит cancel_requested
    через свою (например, HTTP-запрос /runs/{id}/cancel, ADR-0017 §2).
    """
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session_a, factory() as session_b:
        recorder_a = TraceRecorder(session_a)
        recorder_b = TraceRecorder(session_b)
        run_a = await recorder_a.start_run(task="гонка", autonomy="yolo", model="m")

        # Другая сессия ставит флаг на своей копии — session_a об этом не знает.
        run_b = await recorder_b.get_run(run_a.id)
        assert run_b is not None
        await recorder_b.request_cancel(run_b)

        # Локальная копия session_a устарела (без cancel_requested), но
        # merge_run_meta обязан подтянуть актуальный meta перед слиянием.
        await recorder_a.merge_run_meta(run_a, {"phases": {"llm_call": {"ms": 10, "count": 1}}})

        assert run_a.meta["cancel_requested"] is True
        assert run_a.meta["phases"]["llm_call"]["count"] == 1
    await engine.dispose()


async def test_update_progress_preserves_concurrent_cancel_flag_with_cached_tokens(
    tmp_path: Path,
) -> None:
    """Тот же сценарий гонки на пути update_progress с ненулевыми
    cached-токенами — штатный путь у любого провайдера с prompt-кэшем.
    """
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session_a, factory() as session_b:
        recorder_a = TraceRecorder(session_a)
        recorder_b = TraceRecorder(session_b)
        run_a = await recorder_a.start_run(task="гонка кэша", autonomy="yolo", model="m")

        run_b = await recorder_b.get_run(run_a.id)
        assert run_b is not None
        await recorder_b.request_cancel(run_b)

        await recorder_a.update_progress(
            run_a, iterations=1, tokens_used=100, cost_usd=0.02, cached_tokens=40
        )

        assert run_a.meta["cancel_requested"] is True
        assert run_a.meta["cached_tokens"] == 40
        # Refresh обязан быть узким: метрики прогресса не должны откатиться.
        assert run_a.iterations == 1
        assert run_a.tokens_used == 100
        assert run_a.cost_usd == pytest.approx(0.02)
    await engine.dispose()


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


async def test_history_invariant_holds_on_resume_after_approval(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Инвариант не срабатывает ложно: pending_tool_calls доисполняются до
    первого complete() после resume, поэтому пар «вызов ↔ результат» не рвётся.

    Регрессия на блок A §1: проверка обязана стоять ПОСЛЕ доисполнения.
    """
    # Прогон до approval: run уходит в waiting_approval с write-ahead вызовом.
    provider = ScriptedProvider(
        [
            _tool_turn(ToolCallRequest(id="c1", name="high_risk_action", arguments_json="{}")),
            _final("готово"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_HighRiskTool())
    loop = AgentLoop(
        provider,
        registry,
        TraceRecorder(db),
        RuntimeConfig(),
        PolicyEngine(
            autonomy=AutonomyMode.SUPERVISED, policies=PoliciesConfig(), workspace=tmp_path
        ),
        tmp_path,
        model_name="test-model",
    )
    outcome = await loop.run("рискованная задача", AutonomyMode.SUPERVISED)
    assert outcome.state is RunState.WAITING_APPROVAL  # type: ignore[union-attr]

    recorder = TraceRecorder(db)
    approval = (await db.execute(select(Approval))).scalar_one()
    await recorder.decide_approval(approval, approved=True, decided_by="test", reason="ок")

    # После одобрения resume обязан завершиться без HistoryInvariantError.
    run, raw_state = await recorder.load_resumable(outcome.run_id)
    outcome = await loop.resume(run, LoopState.from_dict(raw_state))
    assert outcome.state is RunState.COMPLETED
    assert outcome.error is None
