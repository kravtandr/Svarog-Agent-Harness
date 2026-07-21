"""Хранилище джоб планировщика (блок D §1)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
