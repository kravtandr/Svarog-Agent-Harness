"""Низкоуровневая async-обёртка над git — общая для трёх flow (ADR-0003).

Только локальные операции (init/add/commit/branch/diff/log). Операции с
credentials (push, приватный pull) выполняет host-компонент вне sandbox
(ADR-0002/0006) — они добавляются отдельно во Flow C.
"""

import asyncio
import os
from pathlib import Path

# Host-git hardening (ADR-0015 §0.2): агент может посадить hook/config внутри
# rw-workspace (`.git/hooks/pre-commit`, `.git/config`), а host-side commit
# исполняет их на хосте. Нейтрализуем на КАЖДОМ вызове git:
#  * hooks — не исполнять (`core.hooksPath=/dev/null`);
#  * fsmonitor — не запускать внешний процесс наблюдателя;
#  * global/system config — не читать (env → /dev/null): фильтры/алиасы из
#    ~/.gitconfig не подхватываются host-side.
# Локальный `.git/config` в рамках слоя Tool закрыт denylist'ом (file_tools),
# в рамках Mount — separate-git-dir выносит `.git` за пределы bind-mount.
# Эталон — защита Git admin paths / bare-repo planting в Claude Code.
_HARDENED_GIT_FLAGS = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.ext.allow=never",
)
_HARDENED_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


class GitError(Exception):
    """git-команда завершилась с ненулевым кодом."""


def separate_gitdir_for(repo_path: Path) -> Path:
    """Каталог git-объектов вне рабочего дерева репозитория (ADR-0015 §0.2).

    `<parent>/.gitdirs/<name>` — сосед репозитория, но вне самого дерева
    (и вне будущего bind-mount этого дерева в sandbox).
    """
    repo_path = repo_path.expanduser()
    return repo_path.parent / ".gitdirs" / repo_path.name


class GitRepo:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _hardened_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(_HARDENED_GIT_ENV)
        return env

    async def _git(self, *args: str, check: bool = True) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.path),
            *_HARDENED_GIT_FLAGS,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._hardened_env(),
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        code = proc.returncode or 0
        if check and code != 0:
            raise GitError(f"git {' '.join(args)} → код {code}: {stderr.strip() or stdout.strip()}")
        return code, stdout, stderr

    async def is_repo(self) -> bool:
        code, out, _ = await self._git("rev-parse", "--is-inside-work-tree", check=False)
        return code == 0 and out.strip() == "true"

    async def init(
        self, *, initial_branch: str = "main", separate_git_dir: Path | None = None
    ) -> None:
        """Инициализировать репозиторий; separate_git_dir выносит объекты git
        (hooks/config) за пределы рабочего дерева (ADR-0015 §0.2, слой Mount).

        При separate_git_dir в рабочем дереве остаётся файл-указатель `.git`,
        а hooks/config лежат вне bind-mount и недостижимы из sandbox.
        """
        args = ["init", "-b", initial_branch]
        if separate_git_dir is not None:
            separate_git_dir.parent.mkdir(parents=True, exist_ok=True)
            args.append(f"--separate-git-dir={separate_git_dir}")
        await self._git(*args)

    async def ensure_identity(self, name: str = "Svarog", email: str = "svarog@localhost") -> None:
        """Локальная git-идентичность, если глобальная не настроена (для commit)."""
        code, out, _ = await self._git("config", "user.email", check=False)
        if code != 0 or not out.strip():
            await self._git("config", "user.email", email)
            await self._git("config", "user.name", name)

    async def current_branch(self) -> str:
        _, out, _ = await self._git("rev-parse", "--abbrev-ref", "HEAD")
        return out.strip()

    async def has_commits(self) -> bool:
        code, _, _ = await self._git("rev-parse", "HEAD", check=False)
        return code == 0

    async def create_branch(self, name: str) -> None:
        await self._git("checkout", "-b", name)

    async def checkout(self, name: str) -> None:
        await self._git("checkout", name)

    async def branch_exists(self, name: str) -> bool:
        code, _, _ = await self._git(
            "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False
        )
        return code == 0

    async def add(self, *paths: str) -> None:
        await self._git("add", "--", *paths)

    async def add_all(self) -> None:
        await self._git("add", "-A")

    async def staged_files(self) -> list[str]:
        _, out, _ = await self._git("diff", "--cached", "--name-only")
        return [line for line in out.splitlines() if line]

    async def has_staged_changes(self) -> bool:
        code, _, _ = await self._git("diff", "--cached", "--quiet", check=False)
        return code != 0

    async def read_staged(self, path: str) -> str:
        """Содержимое staged-версии файла (для secret scan перед commit)."""
        code, out, _ = await self._git("show", f":{path}", check=False)
        return out if code == 0 else ""

    async def staged_diff(self) -> str:
        _, out, _ = await self._git("diff", "--cached")
        return out

    async def commit(self, message: str, *, trailers: dict[str, str] | None = None) -> str:
        """Закоммитить staged-изменения; вернуть короткий SHA."""
        full_message = message
        if trailers:
            full_message += "\n\n" + "\n".join(f"{k}: {v}" for k, v in trailers.items())
        await self._git("commit", "-m", full_message)
        _, out, _ = await self._git("rev-parse", "--short", "HEAD")
        return out.strip()

    async def status_porcelain(self) -> list[str]:
        _, out, _ = await self._git("status", "--porcelain")
        return [line for line in out.splitlines() if line]

    async def is_dirty(self) -> bool:
        return bool(await self.status_porcelain())

    async def diff_refs(self, base: str, ref: str) -> str:
        """Diff между двумя рефами (для показа skill proposal, Flow B)."""
        _, out, _ = await self._git("diff", f"{base}..{ref}")
        return out

    async def delete_branch(self, name: str) -> None:
        await self._git("branch", "-D", name)

    async def merge_no_ff(self, ref: str, *, message: str) -> str:
        """Влить ветку в текущую отдельным merge-коммитом; вернуть короткий SHA."""
        await self._git("merge", "--no-ff", ref, "-m", message)
        _, out, _ = await self._git("rev-parse", "--short", "HEAD")
        return out.strip()
