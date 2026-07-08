"""Абстракция execution environment (§6.9, ADR-0002).

Модель адаптирована из hermes-agent `tools/environments/{base,local,docker}.py`
(MIT, NousResearch; анализ — docs/reference-analysis.md): spawn-per-call —
каждая команда исполняется отдельным bash-процессом внутри среды. Session
snapshot, cwd-маркеры и переиспользование контейнеров между процессами
намеренно опущены: в Svarog контейнер живет в рамках одного run, при
приостановке sandbox останавливается, workspace сохраняется на диске
(ADR-0005).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


class SandboxError(Exception):
    """Среда исполнения не смогла подготовиться или выполнить команду."""


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
