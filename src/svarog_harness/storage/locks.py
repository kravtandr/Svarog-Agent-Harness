"""LockBackend (ADR-0007): сериализация критических секций между процессами.

Несколько интерфейсов (CLI, gateway, Telegram) могут работать параллельными
процессами и одновременно применять очередь памяти в один git-репозиторий
(ADR-0004). Без взаимного исключения два `MemoryWriter.drain()` столкнутся на
git-индексе (`index.lock`, чужие staged-изменения в коммите, двойное
применение). LockBackend даёт межпроцессную сериализацию за общим интерфейсом.

MVP-backend — файловый advisory-lock (`fcntl.flock`), per-machine: достаточно
для нескольких процессов на одной машине. Multi-machine — Redis-backend с той
же сигнатурой (`storage:` в конфиге, пост-MVP). Lock освобождается ОС при
смерти процесса, поэтому краш writer'а не оставляет вечную блокировку.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
from abc import ABC, abstractmethod
from pathlib import Path
from types import TracebackType

try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover — не-POSIX платформа
    _HAS_FCNTL = False


class LockGuard(ABC):
    """Async-контекст: `__aenter__` возвращает True, если лок взят, иначе False."""

    @abstractmethod
    async def __aenter__(self) -> bool: ...

    @abstractmethod
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class LockBackend(ABC):
    @abstractmethod
    def guard(self, key: str, *, timeout: float = 0.0, poll: float = 0.1) -> LockGuard:
        """Взаимное исключение по ключу.

        `timeout=0` — одна попытка (не взял → False, без ожидания); `timeout>0`
        — опрос каждые `poll` секунд до успеха или истечения таймаута.
        """


class _FileLockGuard(LockGuard):
    def __init__(self, path: Path, timeout: float, poll: float) -> None:
        self._path = path
        self._timeout = timeout
        self._poll = poll
        self._fd: int | None = None
        self._held = False

    def _try_lock(self) -> bool:
        assert self._fd is not None
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    async def __aenter__(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        waited = 0.0
        while True:
            if await asyncio.to_thread(self._try_lock):
                self._held = True
                return True
            if waited >= self._timeout:
                os.close(self._fd)
                self._fd = None
                return False
            await asyncio.sleep(self._poll)
            waited += self._poll

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._fd is None:
            return
        if self._held:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None
        self._held = False


class _NoopLockGuard(LockGuard):
    """Всегда «берёт» лок — для платформ без flock (single-process деградация)."""

    async def __aenter__(self) -> bool:
        return True

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class FileLockBackend(LockBackend):
    """Advisory-lock файлами в выделенном каталоге (`<state>/locks`)."""

    def __init__(self, lock_dir: Path) -> None:
        self._lock_dir = lock_dir

    def guard(self, key: str, *, timeout: float = 0.0, poll: float = 0.1) -> LockGuard:
        if not _HAS_FCNTL:  # pragma: no cover — не-POSIX платформа
            return _NoopLockGuard()
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return _FileLockGuard(self._lock_dir / f"{digest}.lock", timeout, poll)


def default_lock_backend(db_path: Path) -> LockBackend:
    """Файловый lock-backend в каталоге состояния рядом с БД."""
    return FileLockBackend(db_path.expanduser().parent / "locks")
