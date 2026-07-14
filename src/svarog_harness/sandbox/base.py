"""Абстракция execution environment (§6.9, ADR-0002).

Модель адаптирована из hermes-agent `tools/environments/{base,local,docker}.py`
(MIT, NousResearch; анализ — docs/reference-analysis.md): spawn-per-call —
каждая команда исполняется отдельным bash-процессом внутри среды. Session
snapshot, cwd-маркеры и переиспользование контейнеров между процессами
намеренно опущены: в Svarog контейнер живет в рамках одного run, при
приостановке sandbox останавливается, workspace сохраняется на диске
(ADR-0005).
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# Потолок буфера stderr при стриминге — диагностика, не полный лог.
_STDERR_CAP_BYTES = 64 * 1024


class SandboxError(Exception):
    """Среда исполнения не смогла подготовиться или выполнить команду."""


async def read_stream_tail(reader: asyncio.StreamReader) -> str:
    """Читать поток до EOF, храня не больше _STDERR_CAP_BYTES хвоста."""
    tail = b""
    while True:
        chunk = await reader.read(8192)
        if not chunk:
            return tail.decode(errors="replace")
        tail = (tail + chunk)[-_STDERR_CAP_BYTES:]


@dataclass(frozen=True)
class ExecResult:
    """Результат исполнения команды в среде."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class ExecutionEnvironment(ABC):
    """Среда исполнения shell-команд.

    Жизненный цикл: `start()` → серия `execute()` → `cleanup()`.
    Гарантии изоляции (сеть, файловая система, ресурсы) — свойство
    конкретного backend'а: слой 1 по ADR-0002 обеспечивает docker,
    local-trusted работает без гарантий (§17).
    """

    # start/cleanup не abstract намеренно: local-trusted среде нечего готовить.
    async def start(self) -> None:  # noqa: B027
        """Подготовить среду; для docker — запустить контейнер."""

    @abstractmethod
    async def execute(self, command: str, *, timeout_sec: float) -> ExecResult:
        """Выполнить команду; каждая команда стартует в корне workspace."""

    async def cleanup(self) -> None:  # noqa: B027
        """Освободить ресурсы среды; идемпотентно."""

    async def stream(
        self,
        command: str,
        *,
        timeout_sec: float,
        on_line: Callable[[str], Awaitable[None]],
    ) -> ExecResult:
        """Выполнить команду, отдавая stdout построчно по мере вывода.

        Нужен долгоживущим процессам (внешний агент, ADR-0016): события
        стрима должны попадать в trace и heartbeat до завершения процесса.
        В возвращаемом ExecResult stdout пуст — строки уже отданы в on_line.

        Базовая реализация не стримит (fallback через execute): строки
        отдаются разом после завершения. Backend'ы переопределяют честным
        стримингом.
        """
        result = await self.execute(command, timeout_sec=timeout_sec)
        for line in result.stdout.splitlines():
            await on_line(line)
        return ExecResult(
            exit_code=result.exit_code,
            stdout="",
            stderr=result.stderr,
            timed_out=result.timed_out,
        )
