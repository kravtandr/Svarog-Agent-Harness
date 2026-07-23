"""Flow B: skills-репозиторий (§6.8, ADR-0003).

Изменения скиллов идут только через proposal: отдельная ветка + diff +
секрет-скан перед коммитом; merge в базовую ветку — после человеческого
review (§18). Прямые коммиты агента в активные skills запрещены policy.
"""

import re
import uuid
from dataclasses import dataclass

from svarog_harness.gitflow.commit_gate import commit_guarded
from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.paths import safe_join
from svarog_harness.skills.proposal import SkillProposalRequest

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def proposal_branch_name(skill_name: str) -> str:
    slug = _SLUG_RE.sub("-", skill_name.lower()).strip("-")[:32].strip("-") or "skill"
    return f"svarog/skill/{slug}-{uuid.uuid4().hex[:6]}"


@dataclass(frozen=True)
class ProposalArtifacts:
    branch: str
    base: str
    commit_sha: str
    diff: str


class SkillRepoFlow:
    def __init__(self, repo: GitRepo) -> None:
        self._repo = repo

    @property
    def repo(self) -> GitRepo:
        return self._repo

    async def ready(self) -> bool:
        """Путь САМ является skills-репозиторием и имеет базовый коммит.

        Сравнение toplevel с самим путём здесь обязательно, а не придирка:
        `is_repo()` отвечает «я внутри какого-то рабочего дерева», поэтому
        каталог скиллов, лежащий внутри чужого репозитория (например
        `agent-home/skills` внутри чекаута проекта), прошёл бы проверку. Тогда
        `create_proposal` завёл бы ветку в ЧУЖОМ репозитории, `add_all` смёл бы
        в коммит всё незакоммиченное рабочее дерево человека, а `checkout`
        обратно на базовую ветку выбросил бы его из дерева. Кампания 23.07.2026.
        """
        toplevel = await self._repo.toplevel()
        if toplevel is None or toplevel.resolve() != self._repo.path.resolve():
            return False
        return await self._repo.has_commits()

    async def create_proposal(
        self, request: SkillProposalRequest, *, known_values: frozenset[str] = frozenset()
    ) -> ProposalArtifacts:
        """Материализовать proposal в отдельной ветке; вернуть ветку, base и diff."""
        base = await self._repo.current_branch()
        await self._repo.ensure_identity()
        branch = proposal_branch_name(request.skill_name)
        await self._repo.create_branch(branch)
        try:
            # Defense-in-depth (ADR-0015 §0.1): валидатор и writer не доверяют
            # друг другу — safe_join повторно запирает и skill_name, и ключ files
            # внутри repo. Отказ = исключение, proposal не материализуется.
            skill_root = safe_join(self._repo.path, request.skill_name)
            for rel, content in request.files.items():
                target = safe_join(skill_root, rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            await self._repo.add_all()
            trailers = {"Run-Id": request.source_run_id} if request.source_run_id else None
            sha = await commit_guarded(
                self._repo,
                f"skill proposal: {request.skill_name}",
                known_values=known_values,
                trailers=trailers,
            )
            diff = await self._repo.diff_refs(base, branch)
        except BaseException:
            # Откатить недокоммиченное и убрать пустую ветку (напр. secret scan).
            await self._repo._git("reset", "--hard", check=False)
            await self._repo.checkout(base)
            await self._repo.delete_branch(branch)
            raise
        await self._repo.checkout(base)
        return ProposalArtifacts(branch=branch, base=base, commit_sha=sha, diff=diff)

    async def merge(self, branch: str, *, base: str) -> str:
        """Влить proposal-ветку в базовую (review одобрен) и удалить ветку."""
        await self._repo.checkout(base)
        sha = await self._repo.merge_no_ff(branch, message=f"merge skill proposal {branch}")
        await self._repo.delete_branch(branch)
        return sha

    async def reject(self, branch: str, *, base: str) -> None:
        """Отклонить proposal: удалить ветку, база не меняется."""
        await self._repo.checkout(base)
        await self._repo.delete_branch(branch)
