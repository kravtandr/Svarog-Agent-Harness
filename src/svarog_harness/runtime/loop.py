"""Agent loop v0 (§6.2, §11): линейный run без resume.

build context → LLM → tool calls → observe → iterate. Остановки:
финальный ответ модели (completed), max_iterations, бюджет токенов или
стоимости, переполнение контекста (failed с причиной). Checkpoint/resume —
M2 (ADR-0005), refuel — M3. Каждый шаг пишется в trace через TraceRecorder.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from svarog_harness.config.schema import AutonomyMode, RuntimeConfig
from svarog_harness.llm.provider import ChatMessage, ModelProvider, ToolCallRequest
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


class AgentLoop:
    def __init__(
        self,
        provider: ModelProvider,
        registry: ToolRegistry,
        recorder: TraceRecorder,
        runtime_cfg: RuntimeConfig,
        workspace: Path,
        *,
        model_name: str,
        on_text_delta: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._recorder = recorder
        self._cfg = runtime_cfg
        self._workspace = workspace
        self._model_name = model_name
        self._on_text_delta = on_text_delta
        self._on_tool_call = on_tool_call

    async def run(self, task: str, autonomy: AutonomyMode) -> RunOutcome:
        """Выполнить задачу; режим автономии фиксируется в run (ADR-0010)."""
        run = await self._recorder.start_run(
            task=task, autonomy=autonomy.value, model=self._model_name
        )
        messages = build_initial_messages(task, self._workspace)
        for message in messages:
            await self._recorder.add_message(run, message.role, {"content": message.content})

        iterations = 0
        tokens_used = 0
        cost_usd = 0.0
        final_answer = ""

        try:
            while iterations < self._cfg.max_iterations:
                iterations += 1
                result = await self._provider.complete(
                    messages,
                    self._registry.definitions(),
                    on_text_delta=self._on_text_delta,
                )
                tokens_used += result.usage.total_tokens
                cost_usd += result.cost_usd
                await self._recorder.update_progress(
                    run, iterations=iterations, tokens_used=tokens_used, cost_usd=cost_usd
                )
                messages.append(
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
                    final_answer = result.content
                    await self._recorder.finish_run(run, RunState.COMPLETED)
                    return self._outcome(
                        run, RunState.COMPLETED, final_answer, iterations, tokens_used, cost_usd
                    )

                budget_error = self._budget_exceeded(
                    result.usage.prompt_tokens, tokens_used, cost_usd
                )
                if budget_error is not None:
                    await self._recorder.finish_run(run, RunState.FAILED, error=budget_error)
                    return self._outcome(
                        run, RunState.FAILED, "", iterations, tokens_used, cost_usd, budget_error
                    )

                for call in result.tool_calls:
                    tool_result = await self._execute_tool(run, call)
                    messages.append(
                        ChatMessage(
                            role="tool",
                            content=self._render_tool_result(tool_result),
                            tool_call_id=call.id,
                        )
                    )
                    await self._recorder.add_message(
                        run,
                        "tool",
                        {
                            "tool_call_id": call.id,
                            "content": self._render_tool_result(tool_result),
                        },
                    )

            error = f"достигнут лимит итераций ({self._cfg.max_iterations})"
            await self._recorder.finish_run(run, RunState.FAILED, error=error)
            return self._outcome(run, RunState.FAILED, "", iterations, tokens_used, cost_usd, error)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            await self._recorder.finish_run(run, RunState.FAILED, error=error)
            return self._outcome(run, RunState.FAILED, "", iterations, tokens_used, cost_usd, error)

    def _budget_exceeded(self, prompt_tokens: int, tokens_used: int, cost_usd: float) -> str | None:
        """Проверка бюджетов (§3.7); в v0 превышение — failed, suspend/refuel позже."""
        if prompt_tokens > self._cfg.max_context_tokens:
            return (
                f"контекст превысил лимит: {prompt_tokens} > {self._cfg.max_context_tokens} "
                f"токенов (compaction/refuel — M3)"
            )
        if tokens_used > self._cfg.max_tokens_per_run:
            return f"превышен бюджет токенов run: {tokens_used} > {self._cfg.max_tokens_per_run}"
        if cost_usd > self._cfg.max_cost_usd_per_run:
            return (
                f"превышен бюджет стоимости run: "
                f"${cost_usd:.4f} > ${self._cfg.max_cost_usd_per_run}"
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

        if self._on_tool_call is not None:
            self._on_tool_call(call.name, dict(arguments))
        record = await self._recorder.start_tool_call(
            run, tool_name=call.name, arguments=arguments, risk_level=tool.risk_level.value
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
        final_answer: str,
        iterations: int,
        tokens_used: int,
        cost_usd: float,
        error: str | None = None,
    ) -> RunOutcome:
        return RunOutcome(
            run_id=run.id,
            state=state,
            final_answer=final_answer,
            iterations=iterations,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            error=error,
        )
