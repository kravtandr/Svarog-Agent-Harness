"""Один проход планировщика (блок D §3-§6)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.scheduler.store import JobStore
from svarog_harness.scheduler.ticker import JobRunRequest, tick
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import CronJob, JobOrigin, ScheduleKind

_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _enabled_job(
    db: AsyncSession, tmp_path: Path, *, digest: str = "d"
) -> tuple[JobStore, CronJob]:
    store = JobStore(db)
    job = await store.create(
        name="джоба",
        kind=ScheduleKind.EVERY,
        spec="3600",
        tz="UTC",
        task="задача",
        workspace=str(tmp_path),
        autonomy="supervised",
        config_digest=digest,
        origin=JobOrigin.HUMAN,
        first_run_at=_NOW - timedelta(minutes=1),
    )
    await store.set_enabled(job, True)
    return store, job


async def _never_busy(workspace: str) -> bool:
    return False


async def test_tick_runs_due_job(db: AsyncSession, tmp_path: Path) -> None:
    store, job = await _enabled_job(db, tmp_path)
    seen: list[JobRunRequest] = []

    async def run_job(request: JobRunRequest) -> str:
        seen.append(request)
        return "completed"

    done = await tick(
        store, now=_NOW, current_digest="d", run_job=run_job, workspace_busy=_never_busy
    )

    assert done == [job.id]
    assert len(seen) == 1
    assert seen[0].task == "задача"
    assert seen[0].autonomy == "supervised"
    assert job.run_count == 1
    assert job.last_status == "completed"


async def test_tick_skips_busy_workspace_without_losing_schedule(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Занятый workspace — не ошибка: пропускаем тик, расписание не теряем."""
    store, job = await _enabled_job(db, tmp_path)
    calls: list[JobRunRequest] = []

    async def run_job(request: JobRunRequest) -> str:
        calls.append(request)
        return "completed"

    async def always_busy(workspace: str) -> bool:
        return True

    done = await tick(
        store, now=_NOW, current_digest="d", run_job=run_job, workspace_busy=always_busy
    )

    assert done == []
    assert calls == []
    assert job.enabled is True
    assert job.last_status is not None
    assert "занят" in job.last_status


async def test_tick_disables_job_on_config_drift(db: AsyncSession, tmp_path: Path) -> None:
    """Дайджест конфига разошёлся — джоба отключается, run НЕ создаётся."""
    store, job = await _enabled_job(db, tmp_path, digest="старый")
    calls: list[JobRunRequest] = []

    async def run_job(request: JobRunRequest) -> str:
        calls.append(request)
        return "completed"

    done = await tick(
        store, now=_NOW, current_digest="новый", run_job=run_job, workspace_busy=_never_busy
    )

    assert done == []
    assert calls == []
    assert job.enabled is False
    assert job.last_status is not None
    assert "конфиг" in job.last_status


async def test_tick_records_failure_without_disabling(db: AsyncSession, tmp_path: Path) -> None:
    """Упавшая задача не выключает джобу: расписание продолжает работать."""
    store, job = await _enabled_job(db, tmp_path)

    async def run_job(request: JobRunRequest) -> str:
        raise RuntimeError("провал")

    done = await tick(
        store, now=_NOW, current_digest="d", run_job=run_job, workspace_busy=_never_busy
    )

    assert done == []
    assert job.enabled is True
    assert job.last_status is not None
    assert "ошибка" in job.last_status


# --- Блок D §8: системные джобы ---------------------------------------------


async def test_ensure_system_jobs_is_idempotent(db: AsyncSession, tmp_path: Path) -> None:
    """Повторный старт демона не плодит дубликатов системных джоб."""
    from svarog_harness.scheduler.system_jobs import ensure_system_jobs

    store = JobStore(db)
    first = await ensure_system_jobs(
        store,
        workspace=str(tmp_path),
        autonomy="supervised",
        config_digest="d",
        now=_NOW,
        prune_interval_sec=86_400,
    )
    second = await ensure_system_jobs(
        store,
        workspace=str(tmp_path),
        autonomy="supervised",
        config_digest="d",
        now=_NOW,
        prune_interval_sec=86_400,
    )

    assert len(first) == 1
    assert second == []
    jobs = await store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].origin is JobOrigin.SYSTEM
    assert jobs[0].protected is True
    assert jobs[0].enabled is True
