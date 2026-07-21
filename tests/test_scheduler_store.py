"""Хранилище джоб планировщика (блок D §1)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.scheduler.store import JobStore, ProtectedJobError
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import CronJob, JobOrigin, ScheduleKind


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_cron_job_persists_frozen_rights(db: AsyncSession, tmp_path: Path) -> None:
    """Права джобы (автономия и дайджест конфига) хранятся вместе с ней."""
    due = datetime(2026, 7, 21, 3, 0, tzinfo=UTC)
    job = CronJob(
        name="ночная сводка",
        schedule_kind=ScheduleKind.DAILY_AT,
        schedule_spec="03:00",
        tz="UTC",
        task="собери сводку",
        workspace=str(tmp_path),
        autonomy="supervised",
        config_digest="deadbeef",
        origin=JobOrigin.HUMAN,
        enabled=True,
        next_run_at=due,
    )
    db.add(job)
    await db.commit()

    stored = (await db.execute(select(CronJob))).scalar_one()
    assert stored.autonomy == "supervised"
    assert stored.config_digest == "deadbeef"
    assert stored.origin is JobOrigin.HUMAN
    assert stored.protected is False
    assert stored.run_count == 0


async def test_cron_job_defaults_are_safe(db: AsyncSession, tmp_path: Path) -> None:
    """Джоба по умолчанию выключена и не защищена: активацию делает явный шаг."""
    job = CronJob(
        name="черновик",
        schedule_kind=ScheduleKind.EVERY,
        schedule_spec="3600",
        tz="UTC",
        task="задача",
        workspace=str(tmp_path),
        autonomy="supervised",
        config_digest="d",
        origin=JobOrigin.AGENT,
        next_run_at=datetime(2026, 7, 21, tzinfo=UTC) + timedelta(hours=1),
    )
    db.add(job)
    await db.commit()

    stored = (await db.execute(select(CronJob))).scalar_one()
    assert stored.enabled is False
    assert stored.protected is False
    assert stored.last_status is None


# --- Блок D §3, §5: захват джоб и его свойства ------------------------------


async def _make_job(
    db: AsyncSession, tmp_path: Path, *, due: datetime, enabled: bool = True
) -> tuple["JobStore", CronJob]:
    store = JobStore(db)
    job = await store.create(
        name="джоба",
        kind=ScheduleKind.EVERY,
        spec="3600",
        tz="UTC",
        task="задача",
        workspace=str(tmp_path),
        autonomy="supervised",
        config_digest="d",
        origin=JobOrigin.HUMAN,
        first_run_at=due,
    )
    if enabled:
        await store.set_enabled(job, True)
    return store, job


async def test_claim_due_returns_only_enabled_and_due(db: AsyncSession, tmp_path: Path) -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    store, due_job = await _make_job(db, tmp_path, due=now - timedelta(minutes=1))
    await _make_job(db, tmp_path, due=now + timedelta(hours=1))  # ещё не время
    await _make_job(db, tmp_path, due=now - timedelta(hours=1), enabled=False)  # выключена

    claimed = await store.claim_due(now)
    assert [job.id for job in claimed] == [due_job.id]


async def test_claim_due_is_single_flight(db: AsyncSession, tmp_path: Path) -> None:
    """Второй захват той же джобы в тот же момент ничего не возвращает.

    Это рубеж против двух одновременно работающих демонов (§3).
    """
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    store, _ = await _make_job(db, tmp_path, due=now - timedelta(minutes=1))

    first = await store.claim_due(now)
    second = await store.claim_due(now)

    assert len(first) == 1
    assert second == []


async def test_claim_moves_next_run_forward_without_catchup(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Демон простоял сутки — джоба срабатывает один раз, без догоняющего шторма."""
    long_ago = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    store, job = await _make_job(db, tmp_path, due=long_ago)

    claimed = await store.claim_due(now)
    assert len(claimed) == 1
    assert job.next_run_at.replace(tzinfo=UTC) > now
    assert await store.claim_due(now) == []


async def test_disable_with_reason_stops_job(db: AsyncSession, tmp_path: Path) -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    store, job = await _make_job(db, tmp_path, due=now - timedelta(minutes=1))

    await store.disable_with_reason(job, "конфиг изменился")

    assert job.enabled is False
    assert job.last_status is not None
    assert "конфиг изменился" in job.last_status
    assert await store.claim_due(now) == []


async def test_protected_job_cannot_be_removed(db: AsyncSession, tmp_path: Path) -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    store = JobStore(db)
    job = await store.create(
        name="системная",
        kind=ScheduleKind.EVERY,
        spec="3600",
        tz="UTC",
        task="курирование",
        workspace=str(tmp_path),
        autonomy="supervised",
        config_digest="d",
        origin=JobOrigin.SYSTEM,
        first_run_at=now,
        protected=True,
    )
    with pytest.raises(ProtectedJobError):
        await store.remove(job)
