"""Шов data-plane (ADR-0016): протокол Executor и контракт адаптера агента.

Executor — исполняющая сторона run'а. Нативный `AgentLoop` (runtime/loop.py)
удовлетворяет протоколу структурно; `ExternalAgentExecutor`
(runtime/external.py) гоняет внешний кодинг-агент (Claude Code, Codex,
OpenCode) целиком внутри sandbox — enforcement по границе, а не перехват
каждого шага (tier 1, ADR-0016 §6).
"""

from dataclasses import dataclass, field
from pathlib import PurePosixPath
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
#   text — реплика ассистента; reasoning — thinking-канал модели (в trace
#   не пишется; фолбэк финала, когда модель кладёт ответ только в reasoning —
#   gpt-oss/harmony); tool_call/tool_result — действие агента (коррелируются
#   по call_id); result — финал с итогами usage/cost; opaque — неизвестный
#   тип события, сохраняется raw (forward-compat при дрейфе stream-формата CLI).
AgentEventKind = Literal["text", "reasoning", "tool_call", "tool_result", "result", "opaque"]


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


@dataclass(frozen=True)
class AdapterCapabilities:
    """Что адаптер умеет (ADR-0016 §1): матрица публикуется в доке.

    hooks — permission-мост к Policy Engine (tier 2, §6): без него
    supervised-режим отклоняется fail-closed. resume — продолжение сессии
    агента. mcp — подключение MCP-сервера Svarog (фаза 2).
    """

    hooks: bool = False
    resume: bool = False
    mcp: bool = False


@dataclass(frozen=True)
class AgentAuth:
    """Как агент аутентифицируется к LLM-прокси Svarog (ADR-0016 §3).

    api-key: агент шлёт per-run токен bridge (`proxy_token`), прокси меняет
    его на ключ провайдера host-side. subscription: агент аутентифицируется
    своим OAuth-токеном подписки (`credential`), прокси pass-through.
    """

    base_url: str
    proxy_token: str
    mode: str = "api-key"  # api-key | subscription
    credential: str = ""  # subscription: OAuth-токен подписки


@dataclass(frozen=True)
class AgentLaunch:
    """Параметры одного headless-запуска агента.

    session — resume существующей сессии агента; mcp_config /
    settings_file — пути В КОНТЕЙНЕРЕ (mounts готовит infra, ADR-0016
    §4/§6): конфиг MCP-сервера Svarog и managed-настройки (hooks) с
    высшим приоритетом, которые агент не может переписать из workspace.
    """

    task: str
    session: str | None = None
    mcp_config: str | None = None
    settings_file: str | None = None


class AgentAdapter(Protocol):
    """Контракт внешнего агента (ADR-0016 §1): команда + нормализация стрима.

    Общий знаменатель Claude Code / Codex / OpenCode: headless-запуск,
    JSONL-стрим событий, resume по session id, custom base_url, MCP-клиент.
    """

    @property
    def name(self) -> str:
        """Имя адаптера — идентификатор в конфиге и Run.meta."""
        ...

    @property
    def wire_format(self) -> str:
        """Wire-формат LLM-трафика агента для прокси: anthropic | openai."""
        ...

    def capabilities(self) -> AdapterCapabilities: ...

    def command(self, launch: AgentLaunch) -> list[str]:
        """argv headless-запуска агента."""
        ...

    def parse_event(self, line: str) -> list[AgentEvent]:
        """Нормализовать строку JSONL-стрима; пустой список — строка-шум.

        Одна строка может нести несколько событий (у Claude Code assistant-
        сообщение содержит и text-, и tool_use-блоки). Неизвестный, но
        валидный JSON возвращается событием `opaque` с raw.
        """
        ...

    def base_url_env(self, auth: AgentAuth) -> dict[str, str]:
        """Env, направляющий агента на LLM-прокси Svarog (ADR-0016 §3).

        В api-key режиме `auth.proxy_token` — per-run токен bridge (прокси
        меняет его на ключ host-side); в subscription — `auth.credential`
        отдаётся агенту как его OAuth-токен подписки.
        """
        ...

    def state_dir(self) -> PurePosixPath:
        """Каталог состояния агента в контейнере (~/.claude и т.п.) —
        сюда монтируется persistent agent-state volume (ADR-0016 §5)."""
        ...

    def context_files(self, memory: str, skill_cards: str) -> dict[str, str]:
        """Файлы контекста агента (ADR-0016 §4): относительный путь внутри
        state_dir → содержимое (CLAUDE.md / AGENTS.md); пусто — контекст
        не передаётся."""
        ...

    def provider_files(self, model: str | None) -> dict[str, str]:
        """Файлы конфигурации LLM-провайдера агента: относительный путь внутри
        state_dir → содержимое; пусто — адаптер провайдером не управляет
        (модель/endpoint заданы механизмом самого агента)."""
        ...

    def mcp_client_config(self, url: str, token: str) -> dict[str, dict[str, Any]]:
        """JSON-патчи state-файлов для подключения MCP-клиента агента к мосту.

        Ключ — относительный путь state-файла внутри state_dir, значение
        deep-merge'ится в JSON этого файла. Пусто — адаптер берёт мост иначе
        (claude-code: --mcp-config) или MCP не поддерживает."""
        ...

    def managed_policy(self, mcp_config: str | None, hook_command: str | None) -> str | None:
        """Содержимое managed-настроек агента (ADR-0016 §6) — read-only
        mount с высшим приоритетом; None — агент их не поддерживает."""
        ...

    def managed_policy_path(self) -> PurePosixPath | None:
        """Путь managed-настроек в контейнере; None — не поддерживается."""
        ...
