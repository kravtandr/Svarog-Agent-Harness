"""Agent loop (§6.2, §11): возобновляемый run как state machine (ADR-0005).

build context → LLM → tool calls → observe → iterate; checkpoint после
каждого шага. Остановки: финальный ответ модели → completed; лимиты
итераций/токенов/стоимости/контекста → suspended (возобновляется после
изменения лимитов в конфигурации; compaction/refuel — M3); исключение →
failed. Tool calls фиксируются в checkpoint до исполнения (write-ahead) —
при resume недоисполненные вызовы доисполняются первыми.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from svarog_harness.config.schema import AutonomyMode, RuntimeConfig
from svarog_harness.llm.provider import ChatMessage, ModelProvider, ToolCallRequest
from svarog_harness.policy.engine import PolicyAction, PolicyDecision, PolicyEngine
from svarog_harness.runtime.checkpoint import LoopState
from svarog_harness.runtime.context_builder import build_initial_messages
from svarog_harness.storage.models import Run, RunState
from svarog_harness.tools.base import ToolResult
from svarog_harness.tools.registry import ToolRegistry, UnknownToolError
from svarog_harness.trace.recorder import TraceRecorder


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    state: RunState
    final_answer: str
    iterations: int
    tokens_used: int
    cost_usd: float
    error: str | None = None


class _ApprovalRequiredError(Exception):
    """Policy потребовал approval — run уходит в waiting_approval (ADR-0005)."""

    def __init__(
        self, call: ToolCallRequest, arguments: dict[str, object], decision: PolicyDecision
    ) -> None:
        super().__init__(decision.reason)
        self.call = call
        self.arguments = arguments
        self.decision = decision


class AgentLoop:
    def __init__(
        self,
        provider: ModelProvider,
        registry: ToolRegistry,
        recorder: TraceRecorder,
        runtime_cfg: RuntimeConfig,
        policy: PolicyEngine,
        workspace: Path,
        *,
        model_name: str,
        on_text_delta: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, dict[str, object]], None] | None = None,
        on_notify: Callable[[str, str], None] | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._recorder = recorder
        self._cfg = runtime_cfg
        self._policy = policy
        self._workspace = workspace
        self._model_name = model_name
        self._on_text_delta = on_text_delta
        self._on_tool_call = on_tool_call
        self._on_notify = on_notify

    async def run(self, task: str, autonomy: AutonomyMode) -> RunOutcome:
        """Выполнить задачу; режим автономии фиксируется в run (ADR-0010)."""
        run = await self._recorder.start_run(
            task=task, autonomy=autonomy.value, model=self._model_name
        )
        messages = build_initial_messages(task, self._workspace)
        for message in messages:
            await self._recorder.add_message(run, message.role, {"content": message.content})

        state = LoopState(workspace=self._workspace, messages=messages)
        # Стартовый checkpoint: run возобновляем с первой секунды жизни.
        await self._save_checkpoint(run, state)
        return await self._drive(run, state)

    async def resume(self, run: Run, state: LoopState) -> RunOutcome:
        """Продолжить run из checkpoint (run и state загружает recorder)."""
        await self._recorder.set_run_state(run, RunState.RUNNING, error=None)
        return await self._drive(run, state)

    async def _drive(self, run: Run, state: LoopState) -> RunOutcome:
        try:
            # Write-ahead: доисполнить вызовы, зафиксированные до остановки.
            await self._execute_pending(run, state)

            while state.iterations < self._cfg.max_iterations:
                state.iterations += 1
                result = await self._provider.complete(
                    state.messages,
                    self._registry.definitions(),
                    on_text_delta=self._on_text_delta,
                )
                state.tokens_used += result.usage.total_tokens
                state.cost_usd += result.cost_usd
                await self._recorder.update_progress(
                    run,
                    iterations=state.iterations,
                    tokens_used=state.tokens_used,
                    cost_usd=state.cost_usd,
                )
                state.messages.append(
                    ChatMessage(
                        role="assistant", content=result.content, tool_calls=result.tool_calls
                    )
                )
                await self._recorder.add_message(
                    run,
                    "assistant",
                    {
                        "content": result.content,
                        "tool_calls": [
                            {"id": c.id, "name": c.name, "arguments": c.arguments_json}
                            for c in result.tool_calls
                        ],
                    },
                )

                if not result.tool_calls:
                    await self._recorder.finish_run(run, RunState.COMPLETED)
                    return self._outcome(run, RunState.COMPLETED, state, result.content)

                # Write-ahead: tool calls попадают в checkpoint до исполнения.
                state.pending_tool_calls = result.tool_calls
                await self._save_checkpoint(run, state)

                budget_error = self._budget_exceeded(result.usage.prompt_tokens, state)
                if budget_error is not None:
                    return await self._suspend(run, state, budget_error)

                await self._execute_pending(run, state)

            return await self._suspend(
                run,
                state,
                f"достигнут лимит итераций ({self._cfg.max_iterations}); "
                f"увеличьте runtime.max_iterations и выполните resume",
            )
        except _ApprovalRequiredError as approval:
            return await self._wait_for_approval(run, state, approval)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            await self._recorder.finish_run(run, RunState.FAILED, error=error)
            return self._outcome(run, RunState.FAILED, state, "", error)

    async def _execute_pending(self, run: Run, state: LoopState) -> None:
        """Исполнить write-ahead вызовы; checkpoint после каждого результата."""
        while state.pending_tool_calls:
            call = state.pending_tool_calls[0]
            tool_result = await self._execute_tool(run, call)
            rendered = self._render_tool_result(tool_result)
            state.messages.append(ChatMessage(role="tool", content=rendered, tool_call_id=call.id))
            await self._recorder.add_message(
                run, "tool", {"tool_call_id": call.id, "content": rendered}
            )
            state.pending_tool_calls = state.pending_tool_calls[1:]
            await self._save_checkpoint(run, state)

    async def _save_checkpoint(self, run: Run, state: LoopState) -> None:
        await self._recorder.save_checkpoint(run, iteration=state.iterations, state=state.to_dict())

    async def _suspend(self, run: Run, state: LoopState, reason: str) -> RunOutcome:
        """Приостановка (ADR-0005): checkpoint уже сохранен, состояние — suspended."""
        await self._save_checkpoint(run, state)
        await self._recorder.set_run_state(run, RunState.SUSPENDED, error=reason)
        return self._outcome(run, RunState.SUSPENDED, state, "", reason)

    async def _wait_for_approval(
        self, run: Run, state: LoopState, approval: _ApprovalRequiredError
    ) -> RunOutcome:
        """require_approval: Approval-запрос + waiting_approval (ADR-0005, ADR-0010).

        Вызов остается в pending_tool_calls checkpoint'а — после решения
        человека resume доисполнит его (или вернет отказ модели).
        """
        existing = await self._recorder.find_approval_for_call(run, approval.call.id)
        if existing is None:
            # Approval показывает фактические аргументы, не пересказ агента (§12).
            await self._recorder.create_approval(
                run,
                action_type=approval.decision.action_type,
                payload={
                    "call_id": approval.call.id,
                    "tool": approval.call.name,
                    "arguments": approval.arguments,
                    "reason": approval.decision.reason,
                },
            )
        await self._save_checkpoint(run, state)
        await self._recorder.set_run_state(
            run, RunState.WAITING_APPROVAL, error=approval.decision.reason
        )
        return self._outcome(run, RunState.WAITING_APPROVAL, state, "", approval.decision.reason)

    def _budget_exceeded(self, prompt_tokens: int, state: LoopState) -> str | None:
        """Проверка бюджетов (§3.7): превышение — suspended, не failed."""
        if prompt_tokens > self._cfg.max_context_tokens:
            return (
                f"контекст превысил лимит: {prompt_tokens} > {self._cfg.max_context_tokens} "
                f"токенов (compaction/refuel — M3)"
            )
        if state.tokens_used > self._cfg.max_tokens_per_run:
            return (
                f"превышен бюджет токенов run: {state.tokens_used} > {self._cfg.max_tokens_per_run}"
            )
        if state.cost_usd > self._cfg.max_cost_usd_per_run:
            return (
                f"превышен бюджет стоимости run: "
                f"${state.cost_usd:.4f} > ${self._cfg.max_cost_usd_per_run}"
            )
        return None

    async def _execute_tool(self, run: Run, call: ToolCallRequest) -> ToolResult:
        try:
            arguments = call.parse_arguments()
        except ValueError as exc:
            record = await self._recorder.start_tool_call(
                run, tool_name=call.name, arguments={"_raw": call.arguments_json}, risk_level=None
            )
            result = ToolResult.failure(str(exc))
            await self._recorder.finish_tool_call(record, ok=False, output="", error=result.error)
            return result

        try:
            tool = self._registry.get(call.name)
        except UnknownToolError as exc:
            record = await self._recorder.start_tool_call(
                run, tool_name=call.name, arguments=arguments, risk_level=None
            )
            result = ToolResult.failure(str(exc))
            await self._recorder.finish_tool_call(record, ok=False, output="", error=result.error)
            return result

        decision = self._policy.evaluate(tool, arguments)
        if decision.action is PolicyAction.DENY:
            record = await self._recorder.start_tool_call(
                run,
                tool_name=call.name,
                arguments=arguments,
                risk_level=decision.risk_level.value,
                policy_decision=decision.action.value,
            )
            result = ToolResult.failure(f"запрещено политикой: {decision.reason}")
            await self._recorder.finish_tool_call(
                record, ok=False, output="", error=result.error, denied=True
            )
            return result
        if decision.action is PolicyAction.REQUIRE_APPROVAL:
            raise _ApprovalRequiredError(call, arguments, decision)
        if decision.action is PolicyAction.NOTIFY and self._on_notify is not None:
            self._on_notify(call.name, decision.reason)

        if self._on_tool_call is not None:
            self._on_tool_call(call.name, dict(arguments))
        record = await self._recorder.start_tool_call(
            run,
            tool_name=call.name,
            arguments=arguments,
            risk_level=decision.risk_level.value,
            policy_decision=decision.action.value,
        )
        result = await tool.call(arguments)
        await self._recorder.finish_tool_call(
            record, ok=result.ok, output=result.output, error=result.error
        )
        return result

    @staticmethod
    def _render_tool_result(result: ToolResult) -> str:
        if result.ok:
            return result.output or "(успех, пустой вывод)"
        if result.output:
            return f"ошибка: {result.error}\n{result.output}"
        return f"ошибка: {result.error}"

    def _outcome(
        self,
        run: Run,
        state: RunState,
        loop_state: LoopState,
        final_answer: str,
        error: str | None = None,
    ) -> RunOutcome:
        return RunOutcome(
            run_id=run.id,
            state=state,
            final_answer=final_answer,
            iterations=loop_state.iterations,
            tokens_used=loop_state.tokens_used,
            cost_usd=loop_state.cost_usd,
            error=error,
        )
