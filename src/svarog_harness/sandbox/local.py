"""Local trusted среда (§17): исполнение на хосте без изоляции.

Явный режим без гарантий слоя 1 (ADR-0002) — для доверенных локальных
задач. Timeout обеспечивается убийством группы процессов: отмена
communicate() без killpg оставила бы процесс работать.
"""

import asyncio
import contextlib
import os
import signal
from pathlib import Path

from svarog_harness.sandbox.base import ExecResult, ExecutionEnvironment


class LocalEnvironment(ExecutionEnvironment):
    def __init__(self, workspace: Path, *, env: dict[str, str] | None = None) -> None:
        self.workspace = workspace
        # Явно выданные секреты (ADR-0006); доступны команде поверх окружения хоста.
        self._env = env or {}

    async def execute(self, command: str, *, timeout_sec: float) -> ExecResult:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # своя группа процессов, чтобы убить и потомков
            env={**os.environ, **self._env} if self._env else None,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_sec
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            await proc.wait()
            return ExecResult(exit_code=124, stdout="", stderr="", timed_out=True)

        return ExecResult(
            exit_code=proc.returncode or 0,
            stdout=stdout_bytes.decode(errors="replace"),
            stderr=stderr_bytes.decode(errors="replace"),
        )
