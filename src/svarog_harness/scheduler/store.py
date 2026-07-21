"""Хранилище джоб планировщика с атомарным захватом (блок D §1, §3).

Захват реализован как compare-and-set по `next_run_at` внутри транзакции: это
рубеж против двух одновременно работающих демонов, не полагающийся на внешний
лок.
"""

from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.scheduler.schedule import next_run_after, parse_spec
from svarog_harness.storage.models import CronJob, JobOrigin, ScheduleKind


class JobNotFoundError(Exception):
    """Джоба с таким идентификатором не найдена."""


class ProtectedJobError(Exception):
    """Системная джоба не изменяется и не удаляется извне."""


class JobStore:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(
        self,
        *,
        name: str,
        kind: ScheduleKind,
        spec: str,
        tz: str,
        task: str,
        workspace: str,
        autonomy: str,
        config_digest: str,
        origin: JobOrigin,
        first_run_at: datetime,
        session_id: str | None = None,
        protected: bool = False,
    ) -> CronJob:
        """Завести джобу. Создаётся выключенной: активация — отдельный шаг."""
        parse_spec(kind, spec)
        job = CronJob(
            name=name,
            schedule_kind=kind,
            schedule_spec=spec,
            tz=tz,
            task=task,
            workspace=workspace,
            session_id=session_id,
            autonomy=autonomy,
            config_digest=config_digest,
            origin=origin,
            enabled=False,
            protected=protected,
            next_run_at=first_run_at,
        )
        self._db.add(job)
        await self._db.commit()
        return job

    async def list_jobs(self, *, only_enabled: bool = False) -> list[CronJob]:
        stmt = select(CronJob).order_by(CronJob.next_run_at)
        if only_enabled:
            stmt = stmt.where(CronJob.enabled.is_(True))
        return list((await self._db.execute(stmt)).scalars())

    async def get(self, job_id: str) -> CronJob:
        job = await self._db.get(CronJob, job_id)
        if job is None:
            raise JobNotFoundError(f"джоба не найдена: {job_id}")
        return job

    async def claim_due(self, now: datetime) -> list[CronJob]:
        """Захватить джобы, чьё время пришло, сдвинув их расписание вперёд.

        Сдвиг считается от `now`, а не от просроченного `next_run_at`: демон,
        простоявший сутки, отрабатывает джобу ОДИН раз, без догоняющего шторма
        (at-least-once без catch-up, §5).
        """
        stmt = select(CronJob).where(CronJob.enabled.is_(True), CronJob.next_run_at <= now)
        candidates = list((await self._db.execute(stmt)).scalars())

        claimed: list[CronJob] = []
        for job in candidates:
            following = next_run_after(job.schedule_kind, job.schedule_spec, job.tz, now)
            # compare-and-set: захватывает тот, кто первым сдвинул next_run_at.
            # execute() типизирован как Result; rowcount есть только у CursorResult,
            # который DML и возвращает — отсюда cast.
            result = cast(
                "CursorResult[Any]",
                await self._db.execute(
                    update(CronJob)
                    .where(CronJob.id == job.id, CronJob.next_run_at == job.next_run_at)
                    .values(next_run_at=following)
                ),
            )
            if result.rowcount:
                await self._db.refresh(job)
                claimed.append(job)
        await self._db.commit()
        return claimed

    async def finish(self, job: CronJob, *, status: str, now: datetime) -> None:
        """Записать исход срабатывания."""
        job.last_run_at = now
        job.last_status = status
        job.run_count += 1
        await self._db.commit()

    async def set_enabled(self, job: CronJob, enabled: bool) -> None:
        job.enabled = enabled
        await self._db.commit()

    async def disable_with_reason(self, job: CronJob, reason: str) -> None:
        """Отключить джобу и записать причину — путь fail-closed (§6)."""
        job.enabled = False
        job.last_status = f"отключена: {reason}"
        await self._db.commit()

    async def remove(self, job: CronJob) -> None:
        if job.protected:
            raise ProtectedJobError(f"системная джоба не удаляется: {job.name}")
        await self._db.delete(job)
        await self._db.commit()
