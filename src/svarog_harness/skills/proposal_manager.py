"""Governance-flow skill proposals (§18, Flow B): персист, review, merge.

Заявку агента (`SkillProposalRequest`) менеджер валидирует и материализует в
ветке skills-репозитория (`SkillRepoFlow`), фиксируя метаданные в SQLite.
Решение человека (approve/reject) мержит ветку в базовую или удаляет её.
"""

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.gitflow.commit_gate import SecretScanBlockedError
from svarog_harness.gitflow.repo import GitError, GitRepo
from svarog_harness.gitflow.skill_repo import SkillRepoFlow
from svarog_harness.skills.proposal import SkillProposalRequest, validate_proposal
from svarog_harness.storage.models import SkillProposal, SkillProposalStatus, utcnow


class SkillProposalNotFoundError(Exception):
    def __init__(self, proposal_id: str) -> None:
        super().__init__(f"skill proposal '{proposal_id}' не найден")


class SkillProposalStateError(Exception):
    """Proposal уже разрешён (merged/rejected) — повторное решение недопустимо."""


class SkillProposalManager:
    def __init__(self, db: AsyncSession, skills_dir: Path) -> None:
        self._db = db
        self._skills_dir = skills_dir
        self._flow = SkillRepoFlow(GitRepo(skills_dir))

    async def persist(
        self, request: SkillProposalRequest, *, known_values: frozenset[str] = frozenset()
    ) -> SkillProposal:
        """Провалидировать и материализовать заявку; вернуть записанную строку."""
        errors = validate_proposal(request)
        if errors:
            return await self._record(request, SkillProposalStatus.FAILED, checks=errors)
        if not await self._flow.ready():
            return await self._record(
                request,
                SkillProposalStatus.FAILED,
                checks=[
                    f"'{self._skills_dir}' не является skills-репозиторием: нужен "
                    f"отдельный git-репозиторий с базовым коммитом именно по этому "
                    f"пути. Каталог внутри другого репозитория не подходит — "
                    f"proposal-ветка ушла бы в него"
                ],
            )
        try:
            art = await self._flow.create_proposal(request, known_values=known_values)
        except (SecretScanBlockedError, GitError) as exc:
            return await self._record(request, SkillProposalStatus.FAILED, checks=[str(exc)])
        return await self._record(
            request,
            SkillProposalStatus.PENDING,
            branch=art.branch,
            base=art.base,
            commit_sha=art.commit_sha,
            diff=art.diff,
        )

    async def _record(
        self,
        request: SkillProposalRequest,
        status: SkillProposalStatus,
        *,
        branch: str | None = None,
        base: str | None = None,
        commit_sha: str | None = None,
        diff: str | None = None,
        checks: list[str] | None = None,
    ) -> SkillProposal:
        row = SkillProposal(
            run_id=request.source_run_id,
            skill_name=request.skill_name,
            action=request.action,
            status=status,
            branch=branch,
            base=base,
            commit_sha=commit_sha,
            diff=diff,
            note=request.note or None,
            checks={"validation": checks or []},
        )
        self._db.add(row)
        await self._db.commit()
        return row

    async def list_pending(self, limit: int = 50) -> list[SkillProposal]:
        result = await self._db.execute(
            select(SkillProposal)
            .where(SkillProposal.status == SkillProposalStatus.PENDING)
            .order_by(SkillProposal.created_at)
            .limit(limit)
        )
        return list(result.scalars())

    async def get(self, proposal_id_prefix: str) -> SkillProposal:
        result = await self._db.execute(
            select(SkillProposal).where(SkillProposal.id.startswith(proposal_id_prefix))
        )
        rows = list(result.scalars())
        if not rows:
            raise SkillProposalNotFoundError(proposal_id_prefix)
        if len(rows) > 1:
            raise SkillProposalNotFoundError(f"{proposal_id_prefix} (префикс неоднозначен)")
        return rows[0]

    async def decide(
        self, proposal: SkillProposal, *, approved: bool, decided_by: str, reason: str | None = None
    ) -> str | None:
        """Одобрить (merge в базовую ветку) или отклонить (удалить ветку) proposal."""
        if proposal.status is not SkillProposalStatus.PENDING:
            raise SkillProposalStateError(f"proposal {proposal.id[:8]} уже {proposal.status.value}")
        branch = proposal.branch or ""
        base = proposal.base or "main"
        merged_sha: str | None = None
        if approved:
            merged_sha = await self._flow.merge(branch, base=base)
            proposal.status = SkillProposalStatus.MERGED
            proposal.commit_sha = merged_sha
        else:
            await self._flow.reject(branch, base=base)
            proposal.status = SkillProposalStatus.REJECTED
        proposal.decided_at = utcnow()
        proposal.decided_by = decided_by
        proposal.reason = reason
        await self._db.commit()
        return merged_sha

    @staticmethod
    def validation_messages(proposal: SkillProposal) -> list[str]:
        checks: dict[str, Any] = proposal.checks or {}
        return [str(m) for m in checks.get("validation", [])]
