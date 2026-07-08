"""Single memory writer (ADR-0004): последовательное применение очереди заявок.

Единственный writer применяет MemoryChange-строки из SQLite строго
последовательно и коммитит каждую отдельным коммитом с trailer `Run-Id`.
Конфликты — last-writer-wins (проигравшая версия остаётся в git-истории).
Secret scan обязателен перед каждым коммитом (ADR-0006).
"""

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.gitflow.commit_gate import SecretScanBlockedError, commit_guarded
from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.memory.apply import MemoryApplyError, apply_change
from svarog_harness.memory.change import MemoryChangeRequest
from svarog_harness.storage.models import MemoryChange, MemoryChangeStatus, utcnow


class MemoryWriter:
    """Применяет и коммитит очередь заявок памяти для одного memory-репозитория."""

    def __init__(self, db: AsyncSession, memory_dir: Path) -> None:
        self._db = db
        self._memory_dir = memory_dir
        self._repo = GitRepo(memory_dir)

    async def enqueue(self, request: MemoryChangeRequest) -> MemoryChange:
        row = MemoryChange(
            change=request.to_dict(),
            source_run_id=request.source_run_id,
        )
        self._db.add(row)
        await self._db.commit()
        return row

    async def drain(self, *, known_values: frozenset[str] = frozenset()) -> list[MemoryChange]:
        """Применить все pending-заявки по порядку; вернуть обработанные строки.

        Каждая заявка: применить к файлам → stage → secret scan → commit с
        trailer Run-Id. Ошибка одной заявки помечает её failed и не блокирует
        остальные.
        """
        result = await self._db.execute(
            select(MemoryChange)
            .where(MemoryChange.status == MemoryChangeStatus.PENDING)
            .order_by(MemoryChange.created_at)
        )
        pending = list(result.scalars())
        if not pending:
            return []

        await self._repo.ensure_identity()
        processed: list[MemoryChange] = []
        for row in pending:
            await self._apply_one(row, known_values=known_values)
            processed.append(row)
        return processed

    async def _apply_one(self, row: MemoryChange, *, known_values: frozenset[str]) -> None:
        request = MemoryChangeRequest.from_dict(row.change, source_run_id=row.source_run_id)
        try:
            apply_change(self._memory_dir, request)
            await self._repo.add_all()
            if not await self._repo.has_staged_changes():
                # Заявка не изменила working tree (idempotent) — считаем применённой.
                row.status = MemoryChangeStatus.APPLIED
                row.applied_at = utcnow()
                await self._db.commit()
                return
            trailers = {"Run-Id": row.source_run_id} if row.source_run_id else None
            sha = await commit_guarded(
                self._repo,
                f"memory: {request.summary()}",
                known_values=known_values,
                trailers=trailers,
            )
            row.status = MemoryChangeStatus.APPLIED
            row.applied_at = utcnow()
            row.commit_sha = sha
        except (MemoryApplyError, SecretScanBlockedError, OSError) as exc:
            row.status = MemoryChangeStatus.FAILED
            row.error = str(exc)
        await self._db.commit()
