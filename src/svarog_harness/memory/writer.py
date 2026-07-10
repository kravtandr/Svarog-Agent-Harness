"""Single memory writer (ADR-0004): последовательное применение очереди заявок.

Единственный writer применяет MemoryChange-строки из SQLite строго
последовательно и коммитит каждую отдельным коммитом с trailer `Run-Id`.
Конфликты — last-writer-wins (проигравшая версия остаётся в git-истории).
Secret scan обязателен перед каждым коммитом (ADR-0006).
"""

import contextlib
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.gitflow.commit_gate import SecretScanBlockedError, commit_guarded
from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.memory.apply import MemoryApplyError, apply_change
from svarog_harness.memory.change import MemoryChangeRequest
from svarog_harness.memory.wiki import append_log, log_entry, regenerate_index
from svarog_harness.storage.locks import LockBackend
from svarog_harness.storage.models import MemoryChange, MemoryChangeStatus, utcnow

# Максимальное ожидание writer-лока: если другой процесс дольше держит очередь
# памяти, наши заявки останутся PENDING и применятся следующим drain (память
# eventual, ADR-0004). drain обычно занимает миллисекунды — 30с с запасом.
_DRAIN_LOCK_TIMEOUT = 30.0


class MemoryWriter:
    """Применяет и коммитит очередь заявок памяти для одного memory-репозитория.

    `lock` (ADR-0007) сериализует `drain()` между процессами: несколько
    интерфейсов не должны одновременно коммитить в один memory-репо (ADR-0004).
    None — без блокировки (single-process тесты и утилиты).
    """

    def __init__(
        self, db: AsyncSession, memory_dir: Path, *, lock: LockBackend | None = None
    ) -> None:
        self._db = db
        self._memory_dir = memory_dir
        self._repo = GitRepo(memory_dir)
        self._lock = lock

    async def enqueue(self, request: MemoryChangeRequest) -> MemoryChange:
        row = MemoryChange(
            change=request.to_dict(),
            source_run_id=request.source_run_id,
        )
        self._db.add(row)
        await self._db.commit()
        return row

    async def drain(self, *, known_values: frozenset[str] = frozenset()) -> list[MemoryChange]:
        """Применить все pending-заявки под writer-локом; вернуть обработанные.

        Если лок занят другим процессом — вернуть пустой список, не тронув
        очередь (заявки применит следующий drain).
        """
        if self._lock is None:
            return await self._drain(known_values=known_values)
        key = f"memory-writer:{self._memory_dir.resolve()}"
        async with self._lock.guard(key, timeout=_DRAIN_LOCK_TIMEOUT) as acquired:
            if not acquired:
                return []
            return await self._drain(known_values=known_values)

    async def _drain(self, *, known_values: frozenset[str]) -> list[MemoryChange]:
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
        entries: list[str] = []
        for row in pending:
            entry = await self._apply_one(row, known_values=known_values)
            if entry is not None:
                entries.append(entry)
            processed.append(row)
        await self._reindex(entries, known_values=known_values)
        return processed

    async def _reindex(self, entries: list[str], *, known_values: frozenset[str]) -> None:
        """Автоген index.md/log.md после применённых заявок (ADR-0011).

        Отдельный коммit `memory: reindex`; идемпотентен — если состояние не
        изменилось, staged-изменений нет и коммита не будет.
        """
        append_log(self._memory_dir, entries)
        regenerate_index(self._memory_dir)
        await self._repo.add_all()
        if not await self._repo.has_staged_changes():
            return
        # Автоген собран из уже проверенного контента, поэтому secret-блок здесь
        # крайне маловероятен; но даже он не должен ронять весь drain — оставляем
        # как есть, следующий drain повторит reindex-коммит.
        with contextlib.suppress(SecretScanBlockedError):
            await commit_guarded(self._repo, "memory: reindex", known_values=known_values)

    async def _apply_one(self, row: MemoryChange, *, known_values: frozenset[str]) -> str | None:
        """Применить заявку; вернуть строку журнала (или None, если не применено)."""
        request = MemoryChangeRequest.from_dict(row.change, source_run_id=row.source_run_id)
        try:
            apply_change(self._memory_dir, request)
            await self._repo.add_all()
            if not await self._repo.has_staged_changes():
                # Заявка не изменила working tree (idempotent) — считаем применённой.
                row.status = MemoryChangeStatus.APPLIED
                row.applied_at = utcnow()
                await self._db.commit()
                return None
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
            await self._db.commit()
            return log_entry(
                operation=request.operation.value,
                path=request.file,
                run_id=row.source_run_id,
                when=date.today(),
            )
        except (MemoryApplyError, SecretScanBlockedError, OSError) as exc:
            row.status = MemoryChangeStatus.FAILED
            row.error = str(exc)
            await self._db.commit()
            return None
