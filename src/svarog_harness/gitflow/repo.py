"""Низкоуровневая async-обёртка над git — общая для трёх flow (ADR-0003).

Только локальные операции (init/add/commit/branch/diff/log). Операции с
credentials (push, приватный pull) выполняет host-компонент вне sandbox
(ADR-0002/0006) — они добавляются отдельно во Flow C.
"""

import asyncio
from pathlib import Path


class GitError(Exception):
    """git-команда завершилась с ненулевым кодом."""


class GitRepo:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def _git(self, *args: str, check: bool = True) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.path),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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

    async def init(self, *, initial_branch: str = "main") -> None:
        await self._git("init", "-b", initial_branch)

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
