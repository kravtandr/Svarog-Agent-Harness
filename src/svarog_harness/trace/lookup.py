"""Поиск run по id или уникальному префиксу — общий для viewer, resume и approvals."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.storage.models import Run, Session


class RunNotFoundError(Exception):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"run '{run_id}' не найден (id или его префикс — см. traces list)")


class SessionNotFoundError(Exception):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"session '{session_id}' не найдена (id или префикс — см. sessions list)")


class RunNotResumableError(Exception):
    """Run нельзя возобновить: не тот state или нет checkpoint."""


class ApprovalNotFoundError(Exception):
    def __init__(self, approval_id: str) -> None:
        super().__init__(
            f"approval '{approval_id}' не найден (id или префикс — см. approvals list)"
        )


async def find_run_by_prefix(db: AsyncSession, run_id: str) -> Run:
    result = await db.execute(select(Run).where(Run.id.startswith(run_id)))
    runs = list(result.scalars())
    if not runs:
        raise RunNotFoundError(run_id)
    if len(runs) > 1:
        raise RunNotFoundError(f"{run_id} (префикс неоднозначен: {len(runs)} runs)")
    return runs[0]


async def find_session_by_prefix(db: AsyncSession, session_id: str) -> Session:
    result = await db.execute(select(Session).where(Session.id.startswith(session_id)))
    sessions = list(result.scalars())
    if not sessions:
        raise SessionNotFoundError(session_id)
    if len(sessions) > 1:
        raise SessionNotFoundError(f"{session_id} (префикс неоднозначен: {len(sessions)} sessions)")
    return sessions[0]
