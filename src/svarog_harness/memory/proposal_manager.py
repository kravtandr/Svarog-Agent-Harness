"""Governance memory proposals (блок C, ADR-0020): персист, ревью, применение.

Одобрение не пишет в память напрямую: оно перекладывает заявки в очередь
единственного writer'а (ADR-0004), поэтому применение, secret scan, коммит и
перегенерация index.md идут штатным путём.
"""

from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.memory.apply import MemoryApplyError, preview_content
from svarog_harness.memory.change import MemoryChangeRequest
from svarog_harness.memory.proposal import MemoryProposalRequest, validate_proposal
from svarog_harness.memory.writer import MemoryWriter
from svarog_harness.storage.models import MemoryProposal, MemoryProposalStatus, utcnow

# Потолок показа одной правки: предпросмотр читает человек в терминале, а
# страница памяти может быть большой.
_PREVIEW_LIMIT = 4_000


class MemoryProposalNotFoundError(Exception):
    def __init__(self, proposal_id: str) -> None:
        super().__init__(f"memory proposal '{proposal_id}' не найден")


class MemoryProposalStateError(Exception):
    """Proposal уже разрешён — повторное решение недопустимо."""


class MemoryProposalManager:
    def __init__(self, db: AsyncSession, memory_dir: Path) -> None:
        self._db = db
        self._memory_dir = memory_dir

    async def persist(self, request: MemoryProposalRequest) -> MemoryProposal:
        """Провалидировать и записать proposal; невалидный сохраняется как failed."""
        errors = validate_proposal(self._memory_dir, request)
        head = await GitRepo(self._memory_dir).head_sha()
        row = MemoryProposal(
            run_id=request.source_run_id,
            title=request.title.strip() or "(без названия)",
            rationale=request.rationale,
            changes=request.to_changes_json(),
            status=MemoryProposalStatus.FAILED if errors else MemoryProposalStatus.PENDING,
            memory_head=head,
            checks={"validation": errors},
        )
        self._db.add(row)
        await self._db.commit()
        return row

    async def list_pending(self, limit: int = 50) -> list[MemoryProposal]:
        result = await self._db.execute(
            select(MemoryProposal)
            .where(MemoryProposal.status == MemoryProposalStatus.PENDING)
            .order_by(MemoryProposal.created_at)
            .limit(limit)
        )
        return list(result.scalars())

    async def pending_count(self) -> int:
        result = await self._db.execute(
            select(func.count())
            .select_from(MemoryProposal)
            .where(MemoryProposal.status == MemoryProposalStatus.PENDING)
        )
        return int(result.scalar_one())

    async def get(self, proposal_id_prefix: str) -> MemoryProposal:
        result = await self._db.execute(
            select(MemoryProposal).where(MemoryProposal.id.startswith(proposal_id_prefix))
        )
        rows = list(result.scalars())
        if not rows:
            raise MemoryProposalNotFoundError(proposal_id_prefix)
        if len(rows) > 1:
            raise MemoryProposalNotFoundError(f"{proposal_id_prefix} (префикс неоднозначен)")
        return rows[0]

    async def decide(
        self,
        proposal: MemoryProposal,
        *,
        approved: bool,
        decided_by: str,
        reason: str | None = None,
    ) -> list[str]:
        """Одобрить (в очередь writer'а) или отклонить. Возвращает id заявок."""
        if proposal.status is not MemoryProposalStatus.PENDING:
            raise MemoryProposalStateError(
                f"proposal {proposal.id[:8]} уже {proposal.status.value}"
            )
        change_ids: list[str] = []
        if approved:
            writer = MemoryWriter(self._db, self._memory_dir)
            for raw in proposal.changes:
                request = MemoryChangeRequest.from_dict(raw, source_run_id=proposal.run_id)
                row = await writer.enqueue(request)
                change_ids.append(row.id)
            proposal.status = MemoryProposalStatus.APPLIED
            proposal.applied_change_ids = change_ids
        else:
            proposal.status = MemoryProposalStatus.REJECTED
        proposal.decided_at = utcnow()
        proposal.decided_by = decided_by
        proposal.reason = reason
        await self._db.commit()
        return change_ids

    def preview(self, proposal: MemoryProposal) -> list[tuple[str, str]]:
        """Прогноз содержимого каждого файла на ТЕКУЩЕМ состоянии памяти.

        Замороженный при создании diff устарел бы: между предложением и
        одобрением память меняется. `replace_section` ищет секцию по якорю, а
        `update_field` правит одно поле — обе операции осмысленно
        переприменяются к изменившемуся файлу, поэтому пересчёт честнее снимка.
        """
        previews: list[tuple[str, str]] = []
        for raw in proposal.changes:
            request = MemoryChangeRequest.from_dict(raw)
            try:
                text = preview_content(self._memory_dir, request)
            except MemoryApplyError as exc:
                text = f"(правка больше не применима: {exc})"
            previews.append((request.file, text[:_PREVIEW_LIMIT]))
        return previews

    async def head_moved(self, proposal: MemoryProposal) -> bool:
        """Ушла ли память вперёд с момента предложения."""
        if proposal.memory_head is None:
            return False
        return await GitRepo(self._memory_dir).head_sha() != proposal.memory_head

    @staticmethod
    def validation_messages(proposal: MemoryProposal) -> list[str]:
        checks: dict[str, Any] = proposal.checks or {}
        return [str(m) for m in checks.get("validation", [])]
