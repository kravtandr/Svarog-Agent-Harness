"""Внешний агент как data-plane (ADR-0016, фаза 1 — containment).

Процесс агента исполняется целиком внутри ExecutionEnvironment: границу
держит sandbox (workspace-mount, non-root, лимиты, сеть off до появления
LLM-прокси), а не перехват его tool calls. Стрим stdout нормализуется
адаптером в AgentEvent и пишется тем же TraceRecorder, что у нативного
loop — trace един для обоих executor'ов; redaction применяется к каждому
событию до записи (ADR-0006).
"""

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from svarog_harness.config.schema import AutonomyMode
from svarog_harness.runtime.executor import AgentAdapter, AgentEvent
from svarog_harness.runtime.loop import RunOutcome
from svarog_harness.secrets.redaction import redact
from svarog_harness.storage.models import Run, RunState, ToolCall
from svarog_harness.trace.recorder import TraceRecorder

if TYPE_CHECKING:
    from collections.abc import Callable

    from svarog_harness.llm.provider import ChatMessage
    from svarog_harness.sandbox.base import ExecutionEnvironment

# Метки внешнего executor'а в Run.meta: по ним resume отличает внешний run
# (фаза 3) и trace-viewer показывает исполнителя.
EXECUTOR_META_KEY = "executor"
ADAPTER_META_KEY = "adapter"
AGENT_SESSION_META_KEY = "agent_session_id"


@dataclass
class _StreamState:
    """Наблюдаемое состояние прогона, собираемое из событий стрима."""

    final_answer: str = ""
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
        on_progress: "Callable[[int, int, float, float], None] | None" = None,
        parent_run_id: str | None = None,
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

    async def run(
        self,
        task: str,
        autonomy: AutonomyMode,
        *,
        session_id: str | None = None,
        history: "list[ChatMessage] | None" = None,
    ) -> RunOutcome:
        # history внешнему агенту не передаётся: контекст диалога живёт в его
        # собственной сессии (resume по agent_session_id — фаза 3 ADR-0016).
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

        state = _StreamState()

        async def on_line(line: str) -> None:
            for event in self._adapter.parse_event(line):
                await self._handle_event(run, state, event)

        command = shlex.join(self._adapter.command(task))
        result = await self._environment.stream(
            command, timeout_sec=self._timeout_sec, on_line=on_line
        )

        # Агент завершился, не отчитавшись по начатым tool calls — фиксируем.
        for record in state.pending.values():
            await self._recorder.finish_tool_call(
                record, ok=False, output="", error="агент завершился до tool_result"
            )
        error = self._exit_error(result.exit_code, result.timed_out, result.stderr, state)
        final_state = RunState.COMPLETED if error is None else RunState.FAILED
        await self._recorder.update_progress(
            run,
            iterations=state.num_turns or state.tool_calls,
            tokens_used=state.tokens_used,
            cost_usd=state.cost_usd,
        )
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

    async def _handle_event(self, run: Run, state: _StreamState, event: AgentEvent) -> None:
        if event.session_id is not None and state.agent_session is None:
            state.agent_session = event.session_id
            await self._recorder.merge_run_meta(run, {AGENT_SESSION_META_KEY: event.session_id})
        match event.kind:
            case "text":
                text = self._redact(event.text)
                await self._recorder.add_message(run, "assistant", {"content": text})
                if self._on_text_delta is not None:
                    self._on_text_delta(text)
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
                    output = self._redact(event.text)
                    await self._recorder.finish_tool_call(
                        finished,
                        ok=event.ok,
                        output=output,
                        error=None if event.ok else output or "инструмент вернул ошибку",
                    )
            case "result":
                state.saw_result = True
                state.result_ok = event.ok
                state.final_answer = self._redact(event.text)
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
                    # Доля контекста внешнего агента неизвестна — 0.0.
                    self._on_progress(state.num_turns, state.tokens_used, state.cost_usd, 0.0)
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
