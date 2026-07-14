"""Local trusted среда (§17): исполнение на хосте без изоляции.

Явный режим без гарантий слоя 1 (ADR-0002) — для доверенных локальных
задач. Timeout обеспечивается убийством группы процессов: отмена
communicate() без killpg оставила бы процесс работать.
"""

import asyncio
import contextlib
import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path

from svarog_harness.sandbox.base import ExecResult, ExecutionEnvironment, read_stream_tail


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

    async def stream(
        self,
        command: str,
        *,
        timeout_sec: float,
        on_line: Callable[[str], Awaitable[None]],
    ) -> ExecResult:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # своя группа процессов, чтобы убить и потомков
            env={**os.environ, **self._env} if self._env else None,
        )
        assert proc.stdout is not None and proc.stderr is not None
        stderr_task = asyncio.create_task(read_stream_tail(proc.stderr))
        try:
            async with asyncio.timeout(timeout_sec):
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    await on_line(raw.decode(errors="replace").rstrip("\n"))
                await proc.wait()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            await proc.wait()
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            return ExecResult(exit_code=124, stdout="", stderr="", timed_out=True)
        except asyncio.CancelledError:
            # Управляемая отмена (suspend, ADR-0016 §7): процесс не переживает её.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            await proc.wait()
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            raise
        return ExecResult(exit_code=proc.returncode or 0, stdout="", stderr=await stderr_task)
