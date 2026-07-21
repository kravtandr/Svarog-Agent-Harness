"""Внешний агент как data-plane (ADR-0016, containment).

Процесс агента исполняется целиком внутри ExecutionEnvironment: границу
держит sandbox (workspace-mount, non-root, лимиты, internal-сеть с
единственным hop к bridge-прокси Svarog), а не перехват его tool calls.
Стрим stdout нормализуется адаптером в AgentEvent и пишется тем же
TraceRecorder, что у нативного loop — trace един для обоих executor'ов;
redaction применяется к каждому событию до записи (ADR-0006). Итоги
usage/cost берутся с bridge-прокси (источник истины, §3); стрим агента —
только UX-прогресс.
"""

import asyncio
import contextlib
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from svarog_harness.config.schema import AutonomyMode
from svarog_harness.runtime.bridge import RunBridge
from svarog_harness.runtime.executor import AgentAdapter, AgentEvent, AgentLaunch
from svarog_harness.runtime.loop import RunOutcome
from svarog_harness.sandbox.base import ExecResult
from svarog_harness.secrets.redaction import redact
from svarog_harness.storage.models import Run, RunState, ToolCall
from svarog_harness.trace.recorder import TraceRecorder

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from svarog_harness.llm.provider import ChatMessage
    from svarog_harness.sandbox.base import ExecutionEnvironment


class SuspendSignal(Protocol):
    """Сигнал приостановки run от control-plane bridge (ADR-0016 §7)."""

    suspend: asyncio.Event
    suspend_reason: str


# Правила run'а в самой реплике задачи: конфиг-уровень (AGENTS.md/CLAUDE.md)
# скилловые чеклисты агентов перебивают (кампания 21.07.2026, S11 — агент
# завершал run анонсом/вопросом), prompt-уровень — нет. В trace пишется
# ЧИСТАЯ реплика пользователя, преамбулу видит только агент.
_RUN_RULES_PREAMBLE = (
    "[Правила run'а Svarog] Заверши деливерабл в этом же запуске: ход без "
    "вызова tools — это финальный ответ, продолжения не будет. Вопрос "
    "человеку — только через MCP-tool ask_user (svarog_ask_user); текстом-"
    "вопросом или анонсом будущей работы run не завершай.\n\n"
)

# Метки внешнего executor'а в Run.meta: по ним resume отличает внешний run
# (фаза 3) и trace-viewer показывает исполнителя.
EXECUTOR_META_KEY = "executor"
ADAPTER_META_KEY = "adapter"
AGENT_SESSION_META_KEY = "agent_session_id"


@dataclass
class _StreamState:
    """Наблюдаемое состояние прогона, собираемое из событий стрима."""

    final_answer: str = ""
    # Codex/OpenCode не несут финальный текст в result-событии — фолбэк на
    # последнюю text-реплику ассистента.
    last_text: str = ""
    # Последний фолбэк: gpt-oss/harmony у части провайдеров кладёт ответ
    # только в reasoning-канал, text-событий нет вовсе.
    last_reasoning: str = ""
    result_ok: bool = False
    saw_result: bool = False
    tokens_used: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    tool_calls: int = 0
    agent_session: str | None = None
    pending: dict[str, ToolCall] = field(default_factory=dict)


class ExternalAgentExecutor:
    """Гоняет внешний агент в sandbox и отображает его стрим в trace."""

    def __init__(
        self,
        adapter: AgentAdapter,
        environment: "ExecutionEnvironment",
        recorder: TraceRecorder,
        *,
        workspace: Path,
        timeout_sec: float,
        config_hash: str | None = None,
        secret_values: frozenset[str] = frozenset(),
        on_text_delta: "Callable[[str], None] | None" = None,
        on_tool_call: "Callable[[str, dict[str, object]], None] | None" = None,
        on_run_started: "Callable[[Run], None] | None" = None,
        on_progress: "Callable[[int, int, float, float, int], None] | None" = None,
        parent_run_id: str | None = None,
        bridge: RunBridge | None = None,
        tool_output_limit: int = 20_000,
        mcp_config: str | None = None,
        settings_file: str | None = None,
        suspend_signal: SuspendSignal | None = None,
    ) -> None:
        self._adapter = adapter
        self._environment = environment
        self._recorder = recorder
        self._workspace = workspace
        self._timeout_sec = timeout_sec
        self._config_hash = config_hash
        self._secret_values = secret_values
        self._on_text_delta = on_text_delta
        self._on_tool_call = on_tool_call
        self._on_run_started = on_run_started
        self._on_progress = on_progress
        # Делегация (ADR-0016 фаза 3.5): внешний run как ребёнок нативного.
        self._parent_run_id = parent_run_id
        # Bridge-прокси (§3): источник истины usage/cost и бюджет-стоп.
        self._bridge = bridge
        # §1.2 ADR-0015: tool_result длиннее лимита персистится на диск.
        self._tool_output_limit = tool_output_limit
        # Пути В КОНТЕЙНЕРЕ: конфиг MCP-сервера Svarog (§4) и managed-настройки (§6).
        self._mcp_config = mcp_config
        self._settings_file = settings_file
        # Suspend-сигнал control-plane (§7): approval/ask_user без решения за
        # grace → стрим отменяется, run уходит в waiting_approval.
        self._suspend = suspend_signal

    async def run(
        self,
        task: str,
        autonomy: AutonomyMode,
        *,
        session_id: str | None = None,
        history: "list[ChatMessage] | None" = None,
        agent_session: str | None = None,
    ) -> RunOutcome:
        # history внешнему агенту не передаётся: контекст диалога живёт в его
        # собственной сессии — chat передаёт agent_session предыдущего run'а
        # той же Session (ADR-0016 фаза 3).
        run = await self._recorder.start_run(
            task=self._redact(task),
            autonomy=autonomy.value,
            model=f"external:{self._adapter.name}",
            session_id=session_id,
            config_hash=self._config_hash,
            workspace=str(self._workspace),
            parent_run_id=self._parent_run_id,
        )
        await self._recorder.merge_run_meta(
            run, {EXECUTOR_META_KEY: "external", ADAPTER_META_KEY: self._adapter.name}
        )
        if self._on_run_started is not None:
            self._on_run_started(run)
        await self._recorder.add_message(run, "user", {"content": self._redact(task)})
        return await self._execute(run, task, agent_session)

    async def resume(self, run: Run, prompt: str, *, agent_session: str) -> RunOutcome:
        """Возобновить приостановленный внешний run (ADR-0016 фаза 3).

        Та же Run-запись; сессия агента поднимается `--resume` c prompt'ом-
        решением (approval granted/denied, ответ ask_user, «бюджет поднят»).
        """
        await self._recorder.set_run_state(run, RunState.RUNNING)
        await self._recorder.add_message(run, "user", {"content": self._redact(prompt)})
        return await self._execute(run, prompt, agent_session)

    async def _execute(self, run: Run, task: str, agent_session: str | None) -> RunOutcome:
        state = _StreamState()

        async def on_line(line: str) -> None:
            for event in self._adapter.parse_event(line):
                await self._handle_event(run, state, event)

        # Преамбула только при наличии MCP-канала: без него упоминание
        # ask_user было бы ложью (codex).
        preamble = _RUN_RULES_PREAMBLE if self._adapter.capabilities().mcp else ""
        launch = AgentLaunch(
            task=preamble + task,
            session=agent_session,
            mcp_config=self._mcp_config,
            settings_file=self._settings_file,
        )
        command = shlex.join(self._adapter.command(launch))
        result, gate_suspended = await self._stream_with_suspend(command, on_line)

        # Агент завершился, не отчитавшись по начатым tool calls — фиксируем.
        for record in state.pending.values():
            await self._recorder.finish_tool_call(
                record, ok=False, output="", error="агент завершился до tool_result"
            )
        # Итоги usage/cost — с прокси, если LLM-трафик шёл через него (§3);
        # stream-события агента — только UX-прогресс.
        if self._bridge is not None and self._bridge.usage.requests > 0:
            state.tokens_used = self._bridge.usage.total_tokens
            state.cost_usd = self._bridge.cost_usd()
        budget_exceeded = self._bridge is not None and self._bridge.usage.budget_exceeded
        error = self._exit_error(result.exit_code, result.timed_out, result.stderr, state)
        if gate_suspended:
            # Approval/ask_user без решения за grace (§7): контейнер не живёт
            # часами — run ждёт человека, resume поднимет сессию агента.
            final_state = RunState.WAITING_APPROVAL
            error = self._suspend.suspend_reason if self._suspend is not None else None
        elif budget_exceeded:
            # Родной путь ADR-0005: поднять лимит → svarog resume.
            final_state = RunState.SUSPENDED
            error = (
                "бюджет run исчерпан (enforcement на LLM-прокси, ADR-0016 §3): "
                "поднимите лимит в конфиге и выполните svarog resume"
            )
        else:
            final_state = RunState.COMPLETED if error is None else RunState.FAILED
        await self._recorder.update_progress(
            run,
            iterations=state.num_turns or state.tool_calls,
            tokens_used=state.tokens_used,
            cost_usd=state.cost_usd,
        )
        if final_state in (RunState.SUSPENDED, RunState.WAITING_APPROVAL):
            # Нетерминальный переход (ADR-0005): finished_at не выставляется.
            await self._recorder.set_run_state(run, final_state, error=error)
        else:
            await self._recorder.finish_run(run, final_state, error=error)
        return RunOutcome(
            run_id=run.id,
            state=final_state,
            final_answer=state.final_answer,
            iterations=state.num_turns or state.tool_calls,
            tokens_used=state.tokens_used,
            cost_usd=state.cost_usd,
            error=error,
        )

    async def _stream_with_suspend(
        self, command: str, on_line: "Callable[[str], Awaitable[None]]"
    ) -> tuple[ExecResult, bool]:
        """Стрим агента с гонкой против suspend-сигнала (§7).

        Suspend отменяет стрим (backend'ы убивают процесс при отмене);
        синтетический ExecResult — прогон прерван управляемо, не ошибкой.
        """
        stream_coro = self._environment.stream(
            command, timeout_sec=self._timeout_sec, on_line=on_line
        )
        if self._suspend is None:
            return await stream_coro, False
        stream_task = asyncio.ensure_future(stream_coro)
        suspend_task = asyncio.ensure_future(self._suspend.suspend.wait())
        try:
            done, _ = await asyncio.wait(
                {stream_task, suspend_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            suspend_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await suspend_task
        if stream_task in done:
            # Suspend мог сработать на последних секундах — он приоритетнее.
            return stream_task.result(), self._suspend.suspend.is_set()
        stream_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stream_task
        return ExecResult(exit_code=0, stdout="", stderr=""), True

    async def _handle_event(self, run: Run, state: _StreamState, event: AgentEvent) -> None:
        if event.session_id is not None and state.agent_session is None:
            state.agent_session = event.session_id
            await self._recorder.merge_run_meta(run, {AGENT_SESSION_META_KEY: event.session_id})
        match event.kind:
            case "text":
                text = self._redact(event.text)
                state.last_text = text
                await self._recorder.add_message(run, "assistant", {"content": text})
                if self._on_text_delta is not None:
                    self._on_text_delta(text)
            case "reasoning":
                # Thinking в trace не пишется — только фолбэк финала.
                state.last_reasoning = self._redact(event.text)
            case "tool_call":
                arguments = {k: self._redact_value(v) for k, v in event.arguments.items()}
                record = await self._recorder.start_tool_call(
                    run,
                    tool_name=event.tool_name,
                    arguments=arguments,
                    risk_level=None,
                    # Внутри workspace агент действует без per-tool policy —
                    # граница tier 1 (ADR-0016 §6); в trace это видно явно.
                    policy_decision="external",
                )
                state.tool_calls += 1
                if event.call_id is not None:
                    state.pending[event.call_id] = record
                else:
                    # Без корреляционного id результат не сматчить — закрываем сразу.
                    await self._recorder.finish_tool_call(record, ok=True, output="", error=None)
                if self._on_tool_call is not None:
                    self._on_tool_call(event.tool_name, dict(arguments))
                # Heartbeat lease (ADR-0015 §0.5): активность есть и между result'ами.
                await self._recorder.update_progress(
                    run,
                    iterations=state.tool_calls,
                    tokens_used=state.tokens_used,
                    cost_usd=state.cost_usd,
                )
            case "tool_result":
                finished = (
                    state.pending.pop(event.call_id, None) if event.call_id is not None else None
                )
                if finished is not None:
                    output = self._render_output(run, event)
                    await self._recorder.finish_tool_call(
                        finished,
                        ok=event.ok,
                        output=output,
                        error=None if event.ok else output or "инструмент вернул ошибку",
                    )
            case "result":
                state.saw_result = True
                state.result_ok = event.ok
                state.final_answer = (
                    self._redact(event.text) or state.last_text or state.last_reasoning
                )
                state.tokens_used += event.input_tokens + event.output_tokens
                state.cost_usd += event.cost_usd
                state.num_turns = event.num_turns
                if state.final_answer:
                    # Финал — в trace: write-ahead повтор делегации (ADR-0015
                    # фаза 3) читает результат через last_assistant_text.
                    await self._recorder.add_message(
                        run, "assistant", {"content": state.final_answer}
                    )
                await self._recorder.update_progress(
                    run,
                    iterations=state.num_turns or state.tool_calls,
                    tokens_used=state.tokens_used,
                    cost_usd=state.cost_usd,
                )
                if self._on_progress is not None:
                    # Доля контекста внешнего агента неизвестна — 0.0; своего
                    # учёта cached-токенов у внешнего агента нет — 0.
                    self._on_progress(state.num_turns, state.tokens_used, state.cost_usd, 0.0, 0)
            case "opaque":
                # Forward-compat (ADR-0016 §8): неизвестные события сохраняются
                # raw — дрейф формата виден в trace, а не теряется молча.
                if event.raw is not None:
                    await self._recorder.add_message(
                        run, "system", {"agent_event": self._redact_value(event.raw)}
                    )

    def _exit_error(
        self, exit_code: int, timed_out: bool, stderr: str, state: _StreamState
    ) -> str | None:
        if timed_out:
            return f"внешний агент превысил wall-clock лимит {self._timeout_sec:.0f}с"
        if exit_code != 0:
            tail = self._redact(stderr.strip()[-500:])
            return f"внешний агент завершился с кодом {exit_code}" + (f": {tail}" if tail else "")
        if not state.saw_result:
            return "стрим агента завершился без result-события"
        if not state.result_ok:
            return f"агент сообщил ошибку: {state.final_answer[:200] or 'без описания'}"
        return None

    def _render_output(self, run: Run, event: AgentEvent) -> str:
        """Порядок: redaction → персистенция → усечение (ADR-0015 §1.2).

        Мегабайтный tool_result не раздувает trace: полный (уже
        отредактированный) текст — в .svarog/tool-results/, в trace —
        голова + путь.
        """
        text = self._redact(event.text)
        limit = self._tool_output_limit
        if len(text) <= limit:
            return text
        safe_call_id = re.sub(r"[^A-Za-z0-9._-]", "_", event.call_id or "call")
        rel = Path(".svarog") / "tool-results" / run.id[:8] / f"{safe_call_id}.txt"
        target = self._workspace / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except OSError:
            return text[:limit] + f"\n… [обрезано: {len(text)} символов, лимит {limit}]"
        return f"{text[:limit]}\n… [показано {limit} из {len(text)} символов; полный вывод: {rel}]"

    def _redact(self, text: str) -> str:
        return redact(text, self._secret_values)

    def _redact_value(self, value: object) -> object:
        """Рекурсивная redaction JSON-значений (аргументы tool calls, raw-события)."""
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, dict):
            return {k: self._redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_value(v) for v in value]
        return value
