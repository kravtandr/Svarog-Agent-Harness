"""Регрессия S24: одобренная schedule.create материализуется в cron_jobs на resume.

Баг: `TaskRunner.resume` (internal path) не передавал `schedule_sink` в `build_loop`
и не вызывал `drain_schedule` после resume. Поэтому approve+resume давали run
`completed` с ответом «настроено», но `cron_jobs` оставался пустым — одобренная
заявка терялась (run_once делает это правильно, resume — нет).

Фикс: resume зеркалит run_once — `schedule_sink` передаётся в `build_loop`,
после `loop.resume(...)` вызывается `drain_schedule`. См. orchestrator.py
`TaskRunner.resume` (internal-agent path).
"""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import AutonomyMode
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.runtime import run_assembly
from svarog_harness.runtime.orchestrator import RunHooks, TaskRunner
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import (
    Approval,
    CronJob,
    JobOrigin,
    RunState,
    ScheduleKind,
)
from svarog_harness.trace.recorder import TraceRecorder


def _write_config(ws: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "svarog.db"
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {db_path}\n",
        encoding="utf-8",
    )


def _patch_provider(monkeypatch: pytest.MonkeyPatch, turns: list[CompletionResult]) -> None:
    class ScriptedProvider(ModelProvider):
        def __init__(self) -> None:
            self.turns = list(turns)

        async def complete(
            self,
            messages: list[ChatMessage],
            tools: list[ToolDefinition],
            *,
            on_text_delta: Callable[[str], None] | None = None,
        ) -> CompletionResult:
            return self.turns.pop(0)

    provider = ScriptedProvider()
    monkeypatch.setattr(run_assembly, "default_provider", lambda models_cfg, store=None: provider)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


def _schedule_call(*, daily_at: str = "09:00", tz: str = "Europe/Moscow") -> str:
    import json

    return json.dumps(
        {
            "name": "утренняя сводка",
            "task": "собрать сводку по проектам",
            "daily_at": daily_at,
            "tz": tz,
        }
    )


async def test_approved_schedule_create_materializes_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approve + resume → ровно одна cron-джоба origin=agent, enabled=True (S24)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    # turn 1: агент вызывает schedule_task (critical → waiting_approval);
    # turn 2: финальный ответ после resume.
    _patch_provider(
        monkeypatch,
        [
            CompletionResult(
                content="",
                tool_calls=(
                    ToolCallRequest(id="c1", name="schedule_task", arguments_json=_schedule_call()),
                ),
                usage=Usage(10, 5),
            ),
            CompletionResult(content="Сводка настроена на 9 утра.", usage=Usage(10, 5)),
        ],
    )

    runner = TaskRunner(load_config(project_dir=ws), ws)
    outcome = await runner.run_once("настрой сводку", AutonomyMode.SUPERVISED, hooks=RunHooks())
    assert outcome.state is RunState.WAITING_APPROVAL

    async def approve(db: AsyncSession) -> None:
        recorder = TraceRecorder(db)
        approval = (await db.execute(select(Approval))).scalar_one()
        await recorder.decide_approval(approval, approved=True, decided_by="test", reason="ок")

    await runner.with_db(approve)

    outcome2 = await runner.resume(outcome.run_id, hooks=RunHooks())
    assert outcome2.state is RunState.COMPLETED

    async def fetch_job(db: AsyncSession) -> list[CronJob]:
        result = await db.execute(select(CronJob))
        return list(result.scalars().all())

    jobs = await runner.with_db(fetch_job)
    assert len(jobs) == 1, f"ожидалась ровно одна джоба, получено {len(jobs)} (баг S24)"
    job = jobs[0]
    assert job.origin is JobOrigin.AGENT
    assert job.enabled is True
    assert job.schedule_kind is ScheduleKind.DAILY_AT
    assert job.schedule_spec == "09:00"
    assert job.tz == "Europe/Moscow"


async def test_denied_schedule_create_does_not_materialize_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deny + resume → cron-джоб нет (контракт S18; фикс не сломал deny-путь)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    _patch_provider(
        monkeypatch,
        [
            CompletionResult(
                content="",
                tool_calls=(
                    ToolCallRequest(id="c1", name="schedule_task", arguments_json=_schedule_call()),
                ),
                usage=Usage(10, 5),
            ),
            CompletionResult(content="Понял, не настраиваю.", usage=Usage(10, 5)),
        ],
    )

    runner = TaskRunner(load_config(project_dir=ws), ws)
    outcome = await runner.run_once("настрой сводку", AutonomyMode.SUPERVISED, hooks=RunHooks())
    assert outcome.state is RunState.WAITING_APPROVAL

    async def deny(db: AsyncSession) -> None:
        recorder = TraceRecorder(db)
        approval = (await db.execute(select(Approval))).scalar_one()
        await recorder.decide_approval(approval, approved=False, decided_by="test", reason="нет")

    await runner.with_db(deny)

    outcome2 = await runner.resume(outcome.run_id, hooks=RunHooks())
    assert outcome2.state is RunState.COMPLETED

    async def fetch_job(db: AsyncSession) -> list[CronJob]:
        result = await db.execute(select(CronJob))
        return list(result.scalars().all())

    jobs = await runner.with_db(fetch_job)
    assert jobs == [], f"deny не должен создавать джобу, получено {len(jobs)}"
