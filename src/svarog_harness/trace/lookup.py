"""Поиск run по id или уникальному префиксу — общий для viewer, resume и approvals."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.storage.models import Run


class RunNotFoundError(Exception):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"run '{run_id}' не найден (id или его префикс — см. traces list)")


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
