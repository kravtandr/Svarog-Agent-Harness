"""Agent loop (§6.2, §11): возобновляемый run как state machine (ADR-0005).

build context → LLM → tool calls → observe → iterate; checkpoint после
каждого шага. Остановки: финальный ответ модели → completed; лимиты
итераций/токенов/стоимости/контекста → suspended (возобновляется после
изменения лимитов); порог refuel → suspended со сбросом контекста в
task_state.md (resume пересобирает его с нуля, §6.10); исключение → failed.
Tool calls фиксируются в checkpoint до исполнения (write-ahead) — при resume
недоисполненные вызовы доисполняются первыми.
"""

import asyncio
import contextlib
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from svarog_harness.config.schema import AutonomyMode, RuntimeConfig
from svarog_harness.gitflow.workspace import WorkspaceFlow
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
)
from svarog_harness.policy.engine import PolicyAction, PolicyDecision, PolicyEngine
from svarog_harness.runtime.checkpoint import LoopState
from svarog_harness.runtime.context_builder import build_initial_messages, build_refuel_messages
from svarog_harness.runtime.history_invariant import assert_history_valid
from svarog_harness.runtime.phase_timer import PhaseTimer
from svarog_harness.runtime.refuel import build_task_state, task_state_path
from svarog_harness.secrets import redact
from svarog_harness.storage.models import ApprovalStatus, Run, RunState, utcnow
from svarog_harness.tools.base import Tool, ToolResult, truncate_text
from svarog_harness.tools.guidance import BoundaryKind, note_for
from svarog_harness.tools.registry import ToolRegistry, UnknownToolError
from svarog_harness.tools.user_tools import ASK_USER_TOOL_NAME
from svarog_harness.trace.recorder import TraceRecorder

# Сообщения агенту, когда ответа на ask_user нет (§6.5): не ошибка — сигнал
# продолжать по своему усмотрению, чтобы run не зависал в ожидании человека.
_QUESTION_TIMEOUT_MSG = (
    "пользователь не ответил в отведённое время — продолжай по своему усмотрению, "
    "зафиксировав сделанное допущение"
)
_QUESTION_NO_ANSWER_MSG = (
    "пользователь оставил вопрос без ответа — продолжай по своему усмотрению, "
    "зафиксировав сделанное допущение"
)

# Сколько раз возвращать модели дефектный «финальный» ответ на повтор
# (протёкший tool call, обрезка, пустой ответ), прежде чем сдаться.
_MAX_NUDGES = 2

_LEAK_NUDGE = (
    "Твой предыдущий ответ содержал попытку вызвать инструмент обычным текстом "
    "(например, 'to=functions.<имя> {...}'). Такой вызов НЕ был исполнен: "
    "никакие изменения не применены и ничего не сохранено. Не сообщай о "
    "выполнении действия — повтори вызов через штатный механизм tool calls."
)

_TRUNCATION_NUDGE = (
    "Твой ответ был обрезан по лимиту токенов и не был принят как финальный. "
    "Сформулируй финальный ответ заново и компактнее."
)

_EMPTY_NUDGE = (
    "Ты вернул пустой ответ без вызова tools — он не был принят как финальный. "
    "Если задача выполнена, кратко опиши результат текстом; если нет — "
    "продолжи работу через tools."
)

_SAVED_CONTENT_MARKER = "[содержимое сохранено в файле]"

# Микрокомпакция (ADR-0015 §1.4): маркер очищенного tool-результата и порог,
# ниже которого сообщение не трогается (мелочь чистить бессмысленно).
_CLEARED_RESULT_PREFIX = "[результат инструмента очищен для экономии контекста"
_MICROCOMPACT_MIN_CHARS = 500
# Путь spill-файла из маркера персистенции (§1.2) — очистка ссылается на него.
_SPILL_PATH_RE = re.compile(r"полный вывод: (\S+) — читай read_file частями")

# Детектор затухающей отдачи (§1.6): итерация «без прогресса» — дельта
# полезного вывода меньше этого числа токенов при отсутствии новых успешных
# tool-результатов. Детерминированные счётчики, не LLM-судья.
_STAGNATION_MIN_PROGRESS_TOKENS = 500

_FILE_ACTION_RE = re.compile(
    r"(?iu)(?:созда[йть]|сохрани|запиши|create|write|save).{0,80}"
    r"(?:файл|file|[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8})"
)

_MISSING_TOOL_NUDGE = (
    "Ты сообщил о создании или изменении файла, но в этом run ещё не было "
    "успешного tool-result. Такой финальный ответ не принят: выполни действие "
    "через write_file, edit_file или bash, затем отчитайся."
)


def _rejection_nudge(result: CompletionResult) -> str | None:
    """Причина не принимать ход без tool calls как финальный ответ (или None)."""
    if result.leak_suspected:
        return _LEAK_NUDGE
    if result.finish_reason == "length":
        return _TRUNCATION_NUDGE
    if not result.content.strip():
        return _EMPTY_NUDGE
    return None


def _task_requires_file_action(task: str) -> bool:
    return bool(_FILE_ACTION_RE.search(task))


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    state: RunState
    final_answer: str
    iterations: int
    tokens_used: int
    cost_usd: float
    error: str | None = None


@dataclass(frozen=True)
class _PreparedCall:
    """Вызов, прошедший отбор в параллельный батч (ADR-0015 §1.3)."""

    call: ToolCallRequest
    arguments: dict[str, Any]
    repairs: list[str]
    tool: Tool[Any]
    decision: PolicyDecision


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
        config_hash: str | None = None,
        skill_cards: str = "",
        memory: str = "",
        skill_load_sink: list[tuple[str, str | None]] | None = None,
        memory_sink: list[dict[str, object]] | None = None,
        workspace_flow: WorkspaceFlow | None = None,
        secret_values: frozenset[str] = frozenset(),
        plan_update_sink: list[dict[str, object]] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, dict[str, object]], None] | None = None,
        on_notify: Callable[[str, str], None] | None = None,
        on_run_started: Callable[[Run], None] | None = None,
        on_progress: Callable[[int, int, float, float, int], None] | None = None,
        parent_run_id: str | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._recorder = recorder
        self._cfg = runtime_cfg
        self._policy = policy
        self._workspace = workspace
        self._model_name = model_name
        self._config_hash = config_hash
        self._skill_cards = skill_cards
        self._memory = memory
        self._workspace_flow = workspace_flow
        # Значения секретов для redaction в tool outputs и trace (ADR-0006, §12).
        self._secret_values = secret_values
        # read_skill tool пишет сюда (name, version); loop сливает в SkillLoad.
        self._skill_load_sink = skill_load_sink if skill_load_sink is not None else []
        # remember tool пишет сюда заявки; loop сливает в очередь MemoryChange.
        self._memory_sink = memory_sink if memory_sink is not None else []
        # update_plan tool пишет сюда полный run-local план; loop переносит его в checkpoint.
        self._plan_update_sink = plan_update_sink if plan_update_sink is not None else []
        self._on_text_delta = on_text_delta
        self._on_tool_call = on_tool_call
        self._on_notify = on_notify
        # Cost/context-индикатор (ADR-0015 фаза 5): (итерация, токены за run,
        # стоимость, доля контекста 0..1) после каждого ответа провайдера.
        self._on_progress = on_progress
        self._saved_file_contents: list[str] = []
        # Интерфейсам (gateway/Telegram) нужен run_id сразу после создания run,
        # чтобы подписаться на его события до завершения (§6.1).
        self._on_run_started = on_run_started
        # Дочерний run (ADR-0015 фаза 3): ссылка на родителя в Run.parent_run_id.
        self._parent_run_id = parent_run_id
        # Тайминги фаз хода (блок A §5): __init__ заводит пустой таймер на
        # случай использования без resume(). run() всегда стартует свежий Run
        # без phases (start_run) — там таймер переинициализируется, а не
        # восстанавливается (restore() был бы no-op для нового run'а, но
        # протёк бы накопленное прошлого run'а при переиспользовании
        # экземпляра AgentLoop). resume() поверх этого восстанавливает
        # накопленное из Run.meta["phases"] через restore() — там это осмысленно.
        self._phases = PhaseTimer()

    async def run(
        self,
        task: str,
        autonomy: AutonomyMode,
        *,
        session_id: str | None = None,
        history: list[ChatMessage] | None = None,
    ) -> RunOutcome:
        """Выполнить задачу; режим автономии фиксируется в run (ADR-0010).

        session_id/history — для chat: run включается в общую сессию и видит
        предыдущий диалог (§10.1).
        """
        run = await self._recorder.start_run(
            task=self._redact_text(task),
            autonomy=autonomy.value,
            model=self._model_name,
            session_id=session_id,
            config_hash=self._config_hash,
            workspace=str(self._workspace),
            parent_run_id=self._parent_run_id,
        )
        if self._on_run_started is not None:
            self._on_run_started(run)
        # Свежий Run от start_run никогда не несёт phases — чистый таймер,
        # а не restore() (см. комментарий в __init__).
        self._phases = PhaseTimer()
        messages = build_initial_messages(
            task,
            self._workspace,
            skill_cards=self._skill_cards,
            memory=self._memory,
            history=history,
        )
        for message in messages:
            await self._record_message(run, message.role, {"content": message.content})

        state = LoopState(workspace=self._workspace, messages=messages, task=task)
        # Стартовый checkpoint: run возобновляем с первой секунды жизни.
        await self._save_checkpoint(run, state)
        return await self._drive(run, state)

    async def resume(self, run: Run, state: LoopState) -> RunOutcome:
        """Продолжить run из checkpoint (run и state загружает recorder)."""
        # Восстановить раскрытые deferred-схемы (ADR-0015 фаза 2): реестр
        # собран заново и без этого «забыл» бы загруженное моделью.
        self._registry.restore_loaded(state.loaded_tools)
        # Без dict(...) обёртки — на не-словаре (строка/число из испорченного
        # meta) PhaseTimer.restore делает ранний возврат вместо ValueError/
        # TypeError, которые иначе улетели бы из resume() до try.
        self._phases.restore((run.meta or {}).get("phases", {}))
        await self._recorder.set_run_state(run, RunState.RUNNING, error=None)
        return await self._drive(run, state)

    async def _drive(self, run: Run, state: LoopState) -> RunOutcome:
        try:
            # Write-ahead: доисполнить вызовы, зафиксированные до остановки.
            # tool_exec — та же фаза, что и в основном цикле: охват симметричен
            # вложенным в неё memory_flush/checkpoint. Замер — только если
            # действительно есть что исполнять: иначе счётчик фазы рос бы у
            # run'а без единого write-ahead вызова, хотя события не было.
            if state.pending_tool_calls:
                with self._phases.measure("tool_exec"):
                    await self._execute_pending(run, state)
            # Resume после refuel-приостановки: пересобрать контекст из
            # task_state.md, отбросив прежнюю историю (§6.10, ADR-0005).
            if state.refuel_pending:
                await self._rebuild_after_refuel(run, state)

            # Блок B §2: max_iterations ограничивает СЕГМЕНТ между refuel'ами.
            # Общие стоп-краны — потолок раундов (max_refuel_rounds) и бюджеты
            # токенов и стоимости; state.iterations остаётся тотальным
            # счётчиком для отчётности и trace.
            while state.iterations_since_refuel < self._cfg.max_iterations:
                # Cooperative-cancel (ADR-0017 §2): флаг ставит gateway из
                # другой сессии БД; проверяем на границе итерации — посреди
                # LLM-вызова или tool-исполнения ногу не рвём, checkpoint цел.
                if await self._recorder.cancel_requested(run):
                    await self._save_checkpoint(run, state)
                    await self._recorder.finish_run(run, RunState.CANCELLED)
                    return self._outcome(run, RunState.CANCELLED, state, "")
                state.iterations += 1
                state.iterations_since_refuel += 1
                # Микрокомпакция (§1.4): дешёвый слой без LLM между «всё в
                # контексте» и refuel. Цена — разовая инвалидация префиксного
                # кэша провайдера; порог держит число срабатываний малым.
                # Замер только когда компакция реально сработала — иначе
                # счётчик фазы рос бы на каждой итерации вне зависимости от
                # того, была ли компакция вообще.
                if self._should_microcompact(state):
                    with self._phases.measure("microcompact"):
                        self._microcompact(state)
                stream_callback = None if self._saved_file_contents else self._on_text_delta
                # Инвариант истории (блок A §1): нарушение — баг loop'а, а не
                # дефект модели; падаем громко, историю не правим.
                assert_history_valid(state.messages)
                with self._phases.measure("llm_call"):
                    result = await self._provider.complete(
                        state.messages,
                        self._registry.definitions(),
                        on_text_delta=stream_callback,
                    )
                result_content = self._sanitize_model_content(result.content)
                if stream_callback is None and self._on_text_delta is not None and result_content:
                    self._on_text_delta(result_content)
                state.tokens_used += result.usage.total_tokens
                state.cached_tokens += result.usage.cached_tokens
                state.cost_usd += result.cost_usd
                state.last_prompt_tokens = result.usage.prompt_tokens
                await self._recorder.update_progress(
                    run,
                    iterations=state.iterations,
                    tokens_used=state.tokens_used,
                    cost_usd=state.cost_usd,
                    cached_tokens=state.cached_tokens,
                )
                # Тайминги фаз (блок A §5) — вместе с прогрессом, тем же путём,
                # что и остальные метрики run'а.
                await self._recorder.merge_run_meta(run, {"phases": self._phases.as_meta()})
                if self._on_progress is not None:
                    context_ratio = result.usage.prompt_tokens / self._cfg.max_context_tokens
                    self._on_progress(
                        state.iterations,
                        state.tokens_used,
                        state.cost_usd,
                        context_ratio,
                        state.cached_tokens,
                    )
                state.messages.append(
                    ChatMessage(
                        role="assistant", content=result_content, tool_calls=result.tool_calls
                    )
                )
                await self._record_message(
                    run,
                    "assistant",
                    {
                        "content": result_content,
                        "tool_calls": [
                            {
                                "id": c.id,
                                "name": c.name,
                                "arguments": self._redact_text(c.arguments_json),
                            }
                            for c in result.tool_calls
                        ],
                    },
                )

                if not result.tool_calls:
                    # Дефектный «финальный» ответ (протёкший tool call, обрезка
                    # по токенам, пустота) не принимается — модель получает
                    # корректирующее сообщение и пробует ещё раз.
                    nudge = _rejection_nudge(result)
                    if (
                        nudge is None
                        and not any(message.role == "tool" for message in state.messages)
                        and _task_requires_file_action(state.task)
                    ):
                        nudge = _MISSING_TOOL_NUDGE
                    if nudge is not None and state.nudges < _MAX_NUDGES:
                        state.nudges += 1
                        state.messages.append(ChatMessage(role="user", content=nudge))
                        await self._record_message(run, "user", {"content": nudge})
                        await self._save_checkpoint(run, state)
                        continue
                    await self._recorder.finish_run(run, RunState.COMPLETED)
                    return self._outcome(run, RunState.COMPLETED, state, result_content)

                # Write-ahead: tool calls попадают в checkpoint до исполнения.
                state.pending_tool_calls = result.tool_calls
                await self._save_checkpoint(run, state)

                budget_error = self._budget_exceeded(result.usage.prompt_tokens, state)
                if budget_error is not None:
                    return await self._suspend(run, state, budget_error)

                with self._phases.measure("tool_exec"):
                    had_tool_success = await self._execute_pending(run, state)

                # Детектор затухающей отдачи (§1.6): suspended (решает человек),
                # не failed. Счётчики сбрасываются при suspend — resume после
                # уточнения задачи получает свежее окно.
                stagnation = self._stagnation_reason(state, result, had_tool_success)
                if stagnation is not None:
                    state.stagnation_call_repeats = 0
                    state.stagnation_last_sig = ""
                    state.stagnation_low_progress_iters = 0
                    return await self._suspend(run, state, stagnation)

                # Refuel: порог итераций сегмента достигнут — сбросить контекст
                # в task_state.md. Если потолок автопродолжений не исчерпан, run
                # продолжает себя сам (§6.10, ADR-0005, блок B §3); иначе —
                # приостановка, и продолжение через `svarog resume`.
                if state.iterations_since_refuel >= self._cfg.refuel_after_iterations:
                    if state.refuel_rounds >= self._cfg.max_refuel_rounds:
                        return await self._refuel_suspend(run, state)
                    await self._autocontinue(run, state)
                    continue

            return await self._suspend(
                run,
                state,
                f"достигнут лимит итераций сегмента ({self._cfg.max_iterations}); "
                f"увеличьте runtime.max_iterations и выполните resume",
            )
        except _ApprovalRequiredError as approval:
            return await self._wait_for_approval(run, state, approval)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            await self._recorder.finish_run(run, RunState.FAILED, error=error)
            return self._outcome(run, RunState.FAILED, state, "", error)

    async def _execute_pending(self, run: Run, state: LoopState) -> bool:
        """Исполнить write-ahead вызовы; checkpoint после каждого результата.

        Подряд идущие вызовы с policy-решением ALLOW и is_concurrency_safe
        исполняются параллельным батчем (ADR-0015 §1.3); checkpoint — один на
        батч. Падение посреди батча → resume переисполняет его целиком —
        безопасно, в батче только читающие вызовы. Остальные вызовы — по
        одному, как раньше.

        Возвращает, был ли хотя бы один успешный результат (сырьё для
        детектора затухающей отдачи, §1.6).
        """
        had_success = False
        while state.pending_tool_calls:
            batch = self._concurrency_safe_prefix(run, state.pending_tool_calls)
            if len(batch) >= 2:
                had_success = await self._execute_batch(run, state, batch) or had_success
                continue
            call = state.pending_tool_calls[0]
            tool_result = await self._execute_tool(run, call)
            had_success = had_success or tool_result.ok
            self._note_tool_call_result(state, call, tool_result)
            await self._flush_skill_loads(run)
            # Замер только когда есть заявки remember в очереди — иначе
            # счётчик фазы рос бы даже при полном отсутствии сброса памяти.
            if self._memory_sink:
                with self._phases.measure("memory_flush"):
                    await self._flush_memory(run)
            self._flush_plan_updates(state)
            rendered = self._render_tool_result(run, call, tool_result)
            state.messages.append(ChatMessage(role="tool", content=rendered, tool_call_id=call.id))
            await self._record_message(run, "tool", {"tool_call_id": call.id, "content": rendered})
            state.pending_tool_calls = state.pending_tool_calls[1:]
            with self._phases.measure("checkpoint"):
                await self._save_checkpoint(run, state)
        return had_success

    def _concurrency_safe_prefix(
        self, run: Run, calls: tuple[ToolCallRequest, ...]
    ) -> list["_PreparedCall"]:
        """Префикс подряд идущих вызовов, пригодных для параллельного батча.

        Policy оценивается здесь — последовательно, в исходном порядке, ДО
        партиционирования: require_approval/deny/notify не «уезжают» в батч,
        а останавливают его и обрабатываются последовательным путём.
        """
        batch: list[_PreparedCall] = []
        for call in calls:
            if len(batch) >= self._cfg.max_tool_concurrency:
                break
            try:
                tool = self._registry.get(call.name)
                arguments, repairs = self._registry.prepare_arguments(tool, call)
            except (ValueError, UnknownToolError):
                break
            decision = self._policy.evaluate(tool, arguments)
            if decision.action is not PolicyAction.ALLOW:
                break
            try:
                args = tool.args_model.model_validate(arguments)
            except ValidationError:
                break
            if not tool.is_concurrency_safe(args):
                break
            batch.append(_PreparedCall(call, arguments, repairs, tool, decision))
        return batch

    async def _execute_batch(
        self, run: Run, state: LoopState, batch: list["_PreparedCall"]
    ) -> bool:
        """Исполнить батч читающих вызовов параллельно (asyncio.gather).

        Trace-запись остаётся последовательной: recorder держит одну
        DB-сессию, конкурентный доступ к ней небезопасен — параллелится
        только само исполнение tool.call. Возвращает, был ли успешный результат.
        """
        records = []
        for prepared in batch:
            if self._on_tool_call is not None:
                self._on_tool_call(prepared.call.name, self._redact_json(dict(prepared.arguments)))
            records.append(
                await self._recorder.start_tool_call(
                    run,
                    tool_name=prepared.call.name,
                    arguments=self._traced_arguments(
                        prepared.call, prepared.arguments, prepared.repairs
                    ),
                    risk_level=prepared.decision.risk_level.value,
                    policy_decision=prepared.decision.action.value,
                )
            )
        results = await asyncio.gather(
            *(prepared.tool.call(prepared.arguments) for prepared in batch)
        )
        # Результаты дописываются в исходном порядке вызовов (ADR-0005 не ослабляется).
        had_success = False
        for prepared, record, result in zip(batch, records, results, strict=True):
            had_success = had_success or result.ok
            self._note_tool_call_result(state, prepared.call, result)
            await self._recorder.finish_tool_call(
                record, ok=result.ok, output=result.output, error=result.error
            )
            rendered = self._render_tool_result(run, prepared.call, result)
            state.messages.append(
                ChatMessage(role="tool", content=rendered, tool_call_id=prepared.call.id)
            )
            await self._record_message(
                run, "tool", {"tool_call_id": prepared.call.id, "content": rendered}
            )
        await self._flush_skill_loads(run)
        state.pending_tool_calls = state.pending_tool_calls[len(batch) :]
        await self._save_checkpoint(run, state)
        return had_success

    async def _flush_skill_loads(self, run: Run) -> None:
        """Записать SkillLoad для скиллов, загруженных read_skill (ADR-0009)."""
        while self._skill_load_sink:
            name, version = self._skill_load_sink.pop(0)
            await self._recorder.log_skill_load(run, skill_name=name, skill_version=version)

    async def _flush_memory(self, run: Run) -> None:
        """Поставить заявки remember в очередь single writer'а (ADR-0004)."""
        while self._memory_sink:
            change = self._memory_sink.pop(0)
            await self._recorder.enqueue_memory_change(run, change)

    def _flush_plan_updates(self, state: LoopState) -> None:
        """Применить последний update_plan к checkpoint-состоянию."""
        while self._plan_update_sink:
            update = self._plan_update_sink.pop(0)
            raw_items = update.get("items", [])
            if isinstance(raw_items, list):
                state.plan = [
                    {
                        "id": str(item.get("id", "")),
                        "text": str(item.get("text", "")),
                        "status": str(item.get("status", "")),
                    }
                    for item in raw_items
                    if isinstance(item, dict)
                ]

    async def _save_checkpoint(self, run: Run, state: LoopState) -> None:
        # Загруженные deferred-схемы (ADR-0015 фаза 2) живут в реестре;
        # checkpoint — их единственный носитель между процессами.
        state.loaded_tools = self._registry.loaded_names()
        await self._recorder.save_checkpoint(
            run, iteration=state.iterations, state=self._redact_json(state.to_dict())
        )

    async def _suspend(self, run: Run, state: LoopState, reason: str) -> RunOutcome:
        """Приостановка (ADR-0005): checkpoint уже сохранен, состояние — suspended."""
        await self._save_checkpoint(run, state)
        await self._recorder.set_run_state(run, RunState.SUSPENDED, error=reason)
        return self._outcome(run, RunState.SUSPENDED, state, "", reason)

    async def _write_task_state(self, run: Run, state: LoopState) -> None:
        """Сериализовать состояние задачи в task_state.md и закоммитить (§6.10).

        Вызывается и при приостановке, и при автопродолжении: файл пишется ДО
        сброса истории, поэтому падение процесса между записью и продолжением
        не теряет прогресс — resume поднимет run с уже готовым файлом.
        """
        task_state = build_task_state(state.task, state.messages, state.iterations, plan=state.plan)
        (state.workspace / task_state_path()).write_text(task_state, encoding="utf-8")
        if self._workspace_flow is not None:
            # Коммит task_state.md — лучший-эффорт (не git-репозиторий, секрет-скан…).
            with contextlib.suppress(Exception):
                await self._workspace_flow.commit_step(
                    "svarog refuel: task_state.md", run_id=run.id
                )

    async def _autocontinue(self, run: Run, state: LoopState) -> None:
        """Сбросить контекст и продолжить run без участия человека (блок B §3).

        Порядок как при приостановке: task_state.md пишется и коммитится ДО
        сброса истории. Счётчик раундов увеличивается здесь и обнуляется только
        ручным resume — человек, продолживший run руками, выдаёт новый бюджет.
        """
        await self._write_task_state(run, state)
        state.refuel_rounds += 1
        state.refuel_pending = True
        state.pending_tool_calls = ()
        state.last_prompt_tokens = 0
        # _rebuild_after_refuel снимает refuel_pending, обнуляет счётчик
        # сегмента, пересобирает историю, пишет её в trace и сохраняет checkpoint.
        await self._rebuild_after_refuel(run, state)
        await self._recorder.merge_run_meta(run, {"refuel_rounds": state.refuel_rounds})
        if self._on_notify is not None:
            self._on_notify(
                "refuel",
                f"контекст сброшен в task_state.md, продолжаю "
                f"(раунд {state.refuel_rounds} из {self._cfg.max_refuel_rounds})",
            )

    async def _refuel_suspend(self, run: Run, state: LoopState) -> RunOutcome:
        """Refuel как приостановка (§6.10, ADR-0005): сбросить контекст в
        task_state.md и уйти в suspended.

        Путь для случая, когда автопродолжение выключено (max_refuel_rounds=0)
        или потолок раундов исчерпан. Раздутая история из checkpoint убирается —
        resume пересоберёт контекст с нуля из task_state.md. Процесс и sandbox
        между refuel и resume освобождаются.
        """
        await self._write_task_state(run, state)
        state.refuel_pending = True
        # Раздутую историю в checkpoint не тащим — resume пересоберёт из файла.
        state.messages = []
        state.pending_tool_calls = ()
        state.iterations_since_refuel = 0
        state.last_prompt_tokens = 0
        if self._cfg.max_refuel_rounds:
            reason = (
                f"исчерпан потолок автопродолжений "
                f"(max_refuel_rounds={self._cfg.max_refuel_rounds}); контекст сброшен "
                f"в task_state.md — поднимите потолок или выполните svarog resume"
            )
        else:
            reason = (
                "refuel: контекст сброшен в task_state.md; выполните svarog resume для продолжения"
            )
        return await self._suspend(run, state, reason)

    async def _rebuild_after_refuel(self, run: Run, state: LoopState) -> None:
        """Пересобрать контекст из task_state.md при resume после refuel (§6.10)."""
        task_state_file = state.workspace / task_state_path()
        task_state = task_state_file.read_text(encoding="utf-8") if task_state_file.exists() else ""
        state.messages = build_refuel_messages(
            state.task,
            state.workspace,
            task_state,
            skill_cards=self._skill_cards,
            memory=self._memory,
        )
        state.refuel_pending = False
        state.iterations_since_refuel = 0
        for message in state.messages:
            await self._record_message(run, message.role, {"content": message.content})
        await self._save_checkpoint(run, state)

    def _redact_text(self, text: str) -> str:
        return redact(text, self._secret_values)

    def _redact_json(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, list):
            return [self._redact_json(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self._redact_json(item) for key, item in value.items()}
        return value

    def _traced_arguments(
        self, call: ToolCallRequest, arguments: dict[str, Any], repairs: list[str]
    ) -> dict[str, Any]:
        """Аргументы для trace: при ремонте показываем и оригинал, и результат."""
        traced: dict[str, Any] = self._redact_json(arguments)
        if repairs:
            traced = {
                **traced,
                "_repairs": repairs,
                "_raw": self._redact_text(call.arguments_json),
            }
        return traced

    def _remember_saved_content(self, tool_name: str, arguments: dict[str, Any]) -> None:
        if tool_name != "write_file":
            return
        content = arguments.get("content")
        if isinstance(content, str) and content.strip():
            self._saved_file_contents.append(content.strip())

    def _sanitize_model_content(self, content: str) -> str:
        content = self._redact_text(content)
        for saved in sorted(set(self._saved_file_contents), key=len, reverse=True):
            if len(saved) >= 8 and saved in content:
                content = content.replace(saved, _SAVED_CONTENT_MARKER)
        return content

    async def _record_message(self, run: Run, role: str, content: dict[str, Any]) -> None:
        await self._recorder.add_message(run, role, self._redact_json(content))

    async def _consume_approval(
        self,
        run: Run,
        call: ToolCallRequest,
        arguments: dict[str, Any],
        decision: PolicyDecision,
        repairs: list[str],
    ) -> ToolResult | None:
        """Применить решение человека по approval для этого вызова.

        None — approval одобрен, вызов исполняется дальше; ToolResult —
        отказ (или истечение), который возвращается модели; нет решения —
        run уходит в waiting_approval через _ApprovalRequiredError.
        """
        approval = await self._recorder.find_approval_for_call(run, call.id)
        if approval is None or approval.status is ApprovalStatus.PENDING:
            raise _ApprovalRequiredError(call, arguments, decision)
        if approval.status is ApprovalStatus.APPROVED:
            return None
        reason = approval.reason or "без указания причины"
        verb = "истек" if approval.status is ApprovalStatus.EXPIRED else "отклонен пользователем"
        record = await self._recorder.start_tool_call(
            run,
            tool_name=call.name,
            arguments=self._traced_arguments(call, arguments, repairs),
            risk_level=decision.risk_level.value,
            policy_decision=decision.action.value,
        )
        result = ToolResult.failure(
            f"approval {verb}: {reason}", boundary=BoundaryKind.APPROVAL_DENIED
        )
        await self._recorder.finish_tool_call(
            record, ok=False, output="", error=result.error, denied=True
        )
        return result

    async def _consume_question(
        self,
        run: Run,
        call: ToolCallRequest,
        arguments: dict[str, Any],
        decision: PolicyDecision,
        repairs: list[str],
    ) -> ToolResult | None:
        """ask_user: вернуть ответ человека либо (по таймауту) продолжить (§6.5).

        None — здесь не возвращается: у вопроса всегда есть исход (ответ или
        истечение). Нет решения и дедлайн не наступил → _ApprovalRequiredError
        (waiting_approval).
        """
        approval = await self._recorder.find_approval_for_call(run, call.id)
        if approval is None:
            # Первый заход — создать вопрос и уйти в ожидание ответа.
            raise _ApprovalRequiredError(call, arguments, decision)
        if approval.status is ApprovalStatus.APPROVED:
            answer = (approval.reason or "").strip()
            message = f"ответ пользователя: {answer}" if answer else _QUESTION_NO_ANSWER_MSG
            return await self._record_question_result(
                run, call, arguments, decision, message, repairs
            )
        if approval.status is ApprovalStatus.PENDING:
            if not self._question_deadline_passed(approval):
                raise _ApprovalRequiredError(call, arguments, decision)
            await self._recorder.expire_approval(approval)
            return await self._record_question_result(
                run, call, arguments, decision, _QUESTION_TIMEOUT_MSG, repairs
            )
        # DENIED или EXPIRED — ответа нет, продолжаем по best-guess.
        message = (
            _QUESTION_TIMEOUT_MSG
            if approval.status is ApprovalStatus.EXPIRED
            else _QUESTION_NO_ANSWER_MSG
        )
        return await self._record_question_result(run, call, arguments, decision, message, repairs)

    async def _record_question_result(
        self,
        run: Run,
        call: ToolCallRequest,
        arguments: dict[str, Any],
        decision: PolicyDecision,
        message: str,
        repairs: list[str],
    ) -> ToolResult:
        """Записать исход ask_user в trace и вернуть его модели как результат."""
        record = await self._recorder.start_tool_call(
            run,
            tool_name=call.name,
            arguments=self._traced_arguments(call, arguments, repairs),
            risk_level=decision.risk_level.value,
            policy_decision=decision.action.value,
        )
        result = ToolResult.success(message)
        await self._recorder.finish_tool_call(record, ok=True, output=message, error=None)
        return result

    def _question_deadline_passed(self, approval: Any) -> bool:
        raw = approval.payload.get("deadline")
        if not raw:
            return False
        try:
            deadline = datetime.fromisoformat(str(raw))
        except ValueError:
            return False
        return utcnow() >= deadline

    def _question_deadline(self, arguments: dict[str, Any]) -> datetime:
        timeout = arguments.get("timeout_sec") or self._cfg.ask_user_timeout_sec
        return utcnow() + timedelta(seconds=int(timeout))

    async def _wait_for_approval(
        self, run: Run, state: LoopState, approval: _ApprovalRequiredError
    ) -> RunOutcome:
        """require_approval: Approval-запрос + waiting_approval (ADR-0005, ADR-0010).

        Вызов остается в pending_tool_calls checkpoint'а — после решения
        человека resume доисполнит его (или вернет отказ модели). Для ask_user
        в payload добавляется дедлайн: по нему resume решает, ждать ли дальше
        или продолжать без ответа (§6.5).
        """
        with self._phases.measure("approval_wait"):
            existing = await self._recorder.find_approval_for_call(run, approval.call.id)
            if existing is None:
                # Approval показывает фактические аргументы, не пересказ агента (§12).
                payload: dict[str, Any] = {
                    "call_id": approval.call.id,
                    "tool": approval.call.name,
                    "arguments": self._redact_json(approval.arguments),
                    "reason": self._redact_text(approval.decision.reason),
                }
                if approval.call.name == ASK_USER_TOOL_NAME:
                    payload["question"] = approval.arguments.get("question", "")
                    payload["deadline"] = self._question_deadline(approval.arguments).isoformat()
                await self._recorder.create_approval(
                    run,
                    action_type=approval.decision.action_type,
                    payload=payload,
                )
            await self._save_checkpoint(run, state)
            await self._recorder.set_run_state(
                run, RunState.WAITING_APPROVAL, error=approval.decision.reason
            )
        return self._outcome(run, RunState.WAITING_APPROVAL, state, "", approval.decision.reason)

    def _note_tool_call_result(
        self, state: LoopState, call: ToolCallRequest, result: ToolResult
    ) -> None:
        """Учесть вызов для детектора повторов (§1.6).

        Идентичность — совпадение (name, arguments_json) И результата: поллинг
        с меняющимся выводом повтором не считается.
        """
        raw = "\x00".join(
            (call.name, call.arguments_json, str(result.ok), result.output, result.error or "")
        )
        signature = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if signature == state.stagnation_last_sig:
            state.stagnation_call_repeats += 1
        else:
            state.stagnation_last_sig = signature
            state.stagnation_last_tool = call.name
            state.stagnation_call_repeats = 1

    def _stagnation_reason(
        self, state: LoopState, result: CompletionResult, had_tool_success: bool
    ) -> str | None:
        """Причина увести run в suspended по затухающей отдаче (или None)."""
        threshold = self._cfg.stagnation_repeats
        if state.stagnation_call_repeats >= threshold:
            return (
                f"затухающая отдача: {state.stagnation_call_repeats} идентичных вызова(ов) "
                f"{state.stagnation_last_tool} без прогресса; resume после уточнения задачи"
            )
        if (
            result.usage.completion_tokens < _STAGNATION_MIN_PROGRESS_TOKENS
            and not had_tool_success
        ):
            state.stagnation_low_progress_iters += 1
        else:
            state.stagnation_low_progress_iters = 0
        if state.stagnation_low_progress_iters >= threshold:
            return (
                f"затухающая отдача: {state.stagnation_low_progress_iters} итерации(й) подряд "
                f"без новых успешных tool-результатов и с дельтой вывода "
                f"< {_STAGNATION_MIN_PROGRESS_TOKENS} токенов; resume после уточнения задачи"
            )
        return None

    def _should_microcompact(self, state: LoopState) -> bool:
        threshold = self._cfg.microcompact_threshold_ratio * self._cfg.max_context_tokens
        return state.last_prompt_tokens > threshold

    def _microcompact(self, state: LoopState) -> None:
        """Очистить содержимое старых tool-сообщений маркером (ADR-0015 §1.4).

        Структура истории сохраняется (`role`/`tool_call_id` на месте, меняется
        только content) — provider-совместимость цела. Есть spill-файл из §1.2
        → маркер ссылается на него (данные не теряются), иначе предлагает
        повторить вызов. Полные результаты уже в trace — аудит цел.
        """
        tool_indices = [i for i, m in enumerate(state.messages) if m.role == "tool"]
        keep = self._cfg.microcompact_keep_recent
        candidates = tool_indices[:-keep] if keep else tool_indices
        for index in candidates:
            message = state.messages[index]
            if len(message.content) < _MICROCOMPACT_MIN_CHARS:
                continue
            if message.content.startswith(_CLEARED_RESULT_PREFIX):
                continue
            tool_name = self._tool_name_for(state.messages, index, message.tool_call_id)
            spill = _SPILL_PATH_RE.search(message.content)
            # Компакция — обучающий сигнал, а не молчаливое усечение: маркер
            # говорит, что делать дальше, иначе модель повторяет тот же вызов.
            tail = (
                f"полный вывод: {spill.group(1)} — читай read_file частями (offset/limit)"
                if spill
                else "при необходимости повтори вызов с более узкими параметрами "
                "(путь, паттерн, лимит), а не тот же самый"
            )
            state.messages[index] = replace(
                message,
                content=(
                    f"{_CLEARED_RESULT_PREFIX}: {tool_name}, "
                    f"{len(message.content)} символов. {tail}]"
                ),
            )

    @staticmethod
    def _tool_name_for(messages: list[ChatMessage], index: int, tool_call_id: str | None) -> str:
        """Имя tool для tool-сообщения — из tool_calls предыдущего assistant."""
        for message in reversed(messages[:index]):
            if message.role != "assistant":
                continue
            for call in message.tool_calls:
                if call.id == tool_call_id:
                    return call.name
        return "tool"

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
            tool = self._registry.get(call.name)
        except UnknownToolError as exc:
            record = await self._recorder.start_tool_call(
                run,
                tool_name=call.name,
                arguments={"_raw": self._redact_text(call.arguments_json)},
                risk_level=None,
            )
            result = ToolResult.failure(str(exc))
            await self._recorder.finish_tool_call(record, ok=False, output="", error=result.error)
            return result

        try:
            arguments, repairs = self._registry.prepare_arguments(tool, call)
        except ValueError as exc:
            record = await self._recorder.start_tool_call(
                run,
                tool_name=call.name,
                arguments={"_raw": self._redact_text(call.arguments_json)},
                risk_level=None,
            )
            result = ToolResult.failure(str(exc))
            await self._recorder.finish_tool_call(record, ok=False, output="", error=result.error)
            return result

        decision = self._policy.evaluate(tool, arguments)
        if decision.action is PolicyAction.DENY:
            record = await self._recorder.start_tool_call(
                run,
                tool_name=call.name,
                arguments=self._traced_arguments(call, arguments, repairs),
                risk_level=decision.risk_level.value,
                policy_decision=decision.action.value,
            )
            result = ToolResult.failure(
                f"запрещено политикой: {decision.reason}",
                boundary=BoundaryKind.POLICY_DENY,
            )
            await self._recorder.finish_tool_call(
                record, ok=False, output="", error=result.error, denied=True
            )
            return result
        if decision.action is PolicyAction.REQUIRE_APPROVAL:
            if call.name == ASK_USER_TOOL_NAME:
                verdict = await self._consume_question(run, call, arguments, decision, repairs)
            else:
                verdict = await self._consume_approval(run, call, arguments, decision, repairs)
            if verdict is not None:
                return verdict
        if decision.action is PolicyAction.NOTIFY and self._on_notify is not None:
            self._on_notify(call.name, decision.reason)

        if self._on_tool_call is not None:
            self._on_tool_call(call.name, self._redact_json(dict(arguments)))
        record = await self._recorder.start_tool_call(
            run,
            tool_name=call.name,
            arguments=self._traced_arguments(call, arguments, repairs),
            risk_level=decision.risk_level.value,
            policy_decision=decision.action.value,
        )
        result = await tool.call(arguments)
        if result.ok:
            self._remember_saved_content(call.name, arguments)
        await self._recorder.finish_tool_call(
            record, ok=result.ok, output=result.output, error=result.error
        )
        return result

    def _render_tool_result(self, run: Run, call: ToolCallRequest, result: ToolResult) -> str:
        """Порядок: redaction → персистенция → усечение (ADR-0015 §1.2).

        Длинный вывод не теряется: полный (уже отредактированный — секреты на
        диск не попадают, ADR-0006) текст пишется в .svarog/tool-results/,
        модель получает голову + путь. read_file — исключение (петля
        «Read → файл → Read»): честная обрезка с рецептом offset/limit.
        """
        if result.ok:
            text = result.output or "(успех, пустой вывод)"
        elif result.output:
            text = f"ошибка: {result.error}\n{result.output}"
        else:
            text = f"ошибка: {result.error}"
        if result.boundary is not None:
            # Подсказка — надстройка над enforcement (ADR-0002): объясняет уже
            # принятое решение, ничего не разрешая. Повторяется на каждом
            # отказе — она нужна ровно в момент, когда модель собирается
            # повторить бесполезное действие.
            text = f"{text}\n{note_for(result.boundary)}"
        text = redact(text, self._secret_values)

        limit = self._cfg.tool_output_context_chars
        if len(text) <= limit:
            return text
        if call.name == "read_file":
            return (
                f"{text[:limit]}\n… [вывод обрезан: {len(text)} символов, лимит {limit}; "
                f"читай файл частями: read_file(offset=, limit=)]"
            )
        spill_rel = self._spill_tool_output(run, call, text)
        if spill_rel is None:
            # Не удалось записать (например, read-only workspace) — деградация
            # до прежней обрезки, данные останутся только в trace.
            return truncate_text(text, limit)
        return (
            f"{text[:limit]}\n… [показано {limit} из {len(text)} символов; "
            f"полный вывод: {spill_rel} — читай read_file частями]"
        )

    def _spill_tool_output(self, run: Run, call: ToolCallRequest, text: str) -> str | None:
        """Записать полный вывод в workspace; вернуть относительный путь или None."""
        safe_call_id = re.sub(r"[^A-Za-z0-9._-]", "_", call.id) or "call"
        rel = Path(".svarog") / "tool-results" / run.id[:8] / f"{safe_call_id}.txt"
        target = self._workspace / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except OSError:
            return None
        return str(rel)

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
