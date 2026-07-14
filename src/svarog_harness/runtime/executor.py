"""Шов data-plane (ADR-0016): протокол Executor и контракт адаптера агента.

Executor — исполняющая сторона run'а. Нативный `AgentLoop` (runtime/loop.py)
удовлетворяет протоколу структурно; `ExternalAgentExecutor`
(runtime/external.py) гоняет внешний кодинг-агент (Claude Code, Codex,
OpenCode) целиком внутри sandbox — enforcement по границе, а не перехват
каждого шага (tier 1, ADR-0016 §6).
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from svarog_harness.config.schema import AutonomyMode
from svarog_harness.llm.provider import ChatMessage
from svarog_harness.runtime.loop import RunOutcome


class Executor(Protocol):
    """Исполнитель задачи: native AgentLoop или внешний агент (ADR-0016 §1)."""

    async def run(
        self,
        task: str,
        autonomy: AutonomyMode,
        *,
        session_id: str | None = None,
        history: list[ChatMessage] | None = None,
    ) -> RunOutcome: ...


# Виды нормализованных событий стрима внешнего агента (ADR-0016 §8):
#   text — реплика ассистента; tool_call/tool_result — действие агента
#   (коррелируются по call_id); result — финал с итогами usage/cost;
#   opaque — неизвестный тип события, сохраняется raw (forward-compat
#   при дрейфе stream-формата CLI).
AgentEventKind = Literal["text", "tool_call", "tool_result", "result", "opaque"]


@dataclass(frozen=True)
class AgentEvent:
    """Событие JSONL-стрима внешнего агента, нормализованное адаптером."""

    kind: AgentEventKind
    text: str = ""
    tool_name: str = ""
    call_id: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    session_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    raw: dict[str, Any] | None = None


class AgentAdapter(Protocol):
    """Контракт внешнего агента (ADR-0016 §1): команда + нормализация стрима.

    Общий знаменатель Claude Code / Codex / OpenCode: headless-запуск,
    JSONL-стрим событий, resume по session id. Расширения фаз 2-3
    (context_files, base_url_env, state_dir, managed_policy) добавляются
    в контракт по мере реализации — не заранее.
    """

    @property
    def name(self) -> str:
        """Имя адаптера — идентификатор в конфиге и Run.meta."""
        ...

    def command(self, task: str, *, session: str | None = None) -> list[str]:
        """argv headless-запуска агента; session — resume существующей сессии."""
        ...

    def parse_event(self, line: str) -> list[AgentEvent]:
        """Нормализовать строку JSONL-стрима; пустой список — строка-шум.

        Одна строка может нести несколько событий (у Claude Code assistant-
        сообщение содержит и text-, и tool_use-блоки). Неизвестный, но
        валидный JSON возвращается событием `opaque` с raw.
        """
        ...
