"""Flow C: рабочие репозитории пользователя (§6.8, ADR-0003).

pull перед работой → task branch → commit по шагам → push по режиму
автономии (protected ветки — всегда approval). Секрет-скан обязателен
перед каждым commit и повторно перед push (ADR-0006). Push требует
credentials и выполняется host-компонентом вне sandbox (ADR-0002).
"""

import re
import uuid
from dataclasses import dataclass

from svarog_harness.config.schema import GitConfig
from svarog_harness.gitflow.commit_gate import commit_guarded, scan_ref
from svarog_harness.gitflow.repo import GitError, GitRepo
from svarog_harness.secrets import SecretFinding

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def task_branch_name(task: str) -> str:
    """Имя task-ветки: svarog/<слаг задачи>-<короткий id>."""
    slug = _SLUG_RE.sub("-", task.lower()).strip("-")[:32].strip("-") or "task"
    return f"svarog/{slug}-{uuid.uuid4().hex[:6]}"


@dataclass(frozen=True)
class WorkspacePrep:
    is_git: bool
    branch: str | None = None
    pulled: bool = False
    note: str = ""


class WorkspaceFlow:
    def __init__(self, repo: GitRepo, git_cfg: GitConfig) -> None:
        self._repo = repo
        self._cfg = git_cfg

    async def start(self, task: str) -> WorkspacePrep:
        """Подготовить workspace: pull (если есть remote) + task branch."""
        if not await self._repo.is_repo():
            return WorkspacePrep(is_git=False, note="workspace не является git-репозиторием")
        await self._repo.ensure_identity()

        pulled = False
        note = ""
        if self._cfg.auto_pull and await self._has_remote():
            try:
                await self._repo._git("pull", "--ff-only")
                pulled = True
            except GitError as exc:
                note = f"pull пропущен: {exc}"

        if not await self._repo.has_commits():
            # Пустой репозиторий: ветку создать нельзя до первого коммита.
            return WorkspacePrep(
                is_git=True, branch=await self._repo.current_branch(), pulled=pulled, note=note
            )

        branch = task_branch_name(task)
        await self._repo.create_branch(branch)
        return WorkspacePrep(is_git=True, branch=branch, pulled=pulled, note=note)

    async def commit_step(
        self, message: str, *, run_id: str | None = None, known_values: frozenset[str] = frozenset()
    ) -> str | None:
        """Закоммитить все изменения workspace; None — коммитить нечего."""
        # Служебное дерево runtime (spill tool-результатов, ADR-0015 §1.2)
        # не попадает в коммиты Flow C.
        await self._repo.ensure_excluded(".svarog/")
        await self._repo.add_all()
        if not await self._repo.has_staged_changes():
            return None
        trailers = {"Run-Id": run_id} if run_id else None
        return await commit_guarded(
            self._repo, message, known_values=known_values, trailers=trailers
        )

    async def push_precheck(
        self, branch: str, *, known_values: frozenset[str] = frozenset()
    ) -> list[SecretFinding]:
        """Повторный secret scan содержимого ветки перед push (вторая линия)."""
        return await scan_ref(self._repo, branch, known_values=known_values)

    async def push(self, branch: str, *, remote: str = "origin") -> str:
        """Протолкнуть ветку в remote (host-компонент, ADR-0002/0006)."""
        _, out, err = await self._repo._git("push", "-u", remote, branch)
        return (out or err).strip()

    async def _has_remote(self, name: str = "origin") -> bool:
        code, out, _ = await self._repo._git("remote", check=False)
        return code == 0 and name in out.split()
