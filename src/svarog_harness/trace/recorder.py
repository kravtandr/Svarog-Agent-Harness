"""Запись trace в storage (§6.12, §15): сообщения, tool calls, прогресс run'а.

Recorder — единственное место, где agent loop пишет в БД; сам loop
не знает про SQLAlchemy-модели напрямую.
"""

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.runtime.config_snapshot import CONFIG_HASH_META_KEY
from svarog_harness.storage.models import (
    Approval,
    ApprovalStatus,
    Checkpoint,
    CheckResult,
    CheckStatus,
    MemoryChange,
    Message,
    Run,
    RunState,
    Session,
    SkillLoad,
    ToolCall,
    ToolCallStatus,
    utcnow,
)
from svarog_harness.trace.lookup import (
    ApprovalNotFoundError,
    RunNotResumableError,
    find_run_by_prefix,
)

# Состояния, из которых run можно возобновить (ADR-0005).
_RESUMABLE_STATES = frozenset({RunState.SUSPENDED, RunState.WAITING_APPROVAL})

# Cooperative-cancel (ADR-0017 §2): флаг в Run.meta, который loop читает на
# границе итерации; checkpoint сохраняется, run уходит в CANCELLED.
CANCEL_META_KEY = "cancel_requested"

# Per-workspace lease (ADR-0015 §0.5): живой run бьётся heartbeat'ом каждую
# итерацию. Если heartbeat старше этого порога — процесс run'а считается
# мёртвым (упал/завис), lease протух: recovery приостанавливает такой run, а
# новый run на том же workspace допускается. Порог с запасом на медленный
# LLM-вызов между итерациями.
_HEARTBEAT_STALE_SEC = 300


class WorkspaceBusyError(Exception):
    """На workspace уже есть живой (heartbeat свежий) RUNNING run (ADR-0015 §0.5)."""


class TraceRecorder:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._message_index: dict[str, int] = {}

    async def start_run(
        self,
        *,
        task: str,
        autonomy: str,
        model: str,
        session_id: str | None = None,
        config_hash: str | None = None,
        workspace: str | None = None,
        parent_run_id: str | None = None,
    ) -> Run:
        # config_hash — снимок security-конфига run'а (ADR-0015 §0.4): resume
        # сверяет с ним текущий конфиг и fail-closed при расхождении.
        meta: dict[str, object] = {"model": model}
        if config_hash is not None:
            meta[CONFIG_HASH_META_KEY] = config_hash
        now = utcnow()
        # chat переиспользует одну Session на серию runs (§10.1, ADR-0008).
        if session_id is None:
            session = Session(title=task[:200])
            self._db.add(session)
            run = Run(
                session=session,
                state=RunState.RUNNING,
                task=task,
                autonomy=autonomy,
                started_at=now,
                workspace=workspace,
                heartbeat_at=now,
                parent_run_id=parent_run_id,
                meta=meta,
            )
        else:
            run = Run(
                session_id=session_id,
                state=RunState.RUNNING,
                task=task,
                autonomy=autonomy,
                started_at=now,
                workspace=workspace,
                heartbeat_at=now,
                parent_run_id=parent_run_id,
                meta=meta,
            )
        self._db.add(run)
        await self._db.flush()
        self._message_index[run.id] = 0
        await self._db.commit()
        return run

    async def add_message(self, run: Run, role: str, content: dict[str, Any]) -> Message:
        index = await self._next_message_index(run)
        message = Message(run_id=run.id, index_in_run=index, role=role, content=content)
        self._db.add(message)
        await self._db.commit()
        return message

    async def _next_message_index(self, run: Run) -> int:
        """Сквозная нумерация сообщений run'а; при resume продолжается из БД."""
        if run.id not in self._message_index:
            result = await self._db.execute(
                select(func.max(Message.index_in_run)).where(Message.run_id == run.id)
            )
            max_index = result.scalar()
            self._message_index[run.id] = 0 if max_index is None else max_index + 1
        index = self._message_index[run.id]
        self._message_index[run.id] = index + 1
        return index

    async def start_tool_call(
        self,
        run: Run,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        risk_level: str | None,
        policy_decision: str | None = None,
    ) -> ToolCall:
        tool_call = ToolCall(
            run_id=run.id,
            tool_name=tool_name,
            arguments=arguments,
            risk_level=risk_level,
            policy_decision=policy_decision,
            status=ToolCallStatus.RUNNING,
            started_at=utcnow(),
        )
        self._db.add(tool_call)
        await self._db.commit()
        return tool_call

    async def finish_tool_call(
        self, tool_call: ToolCall, *, ok: bool, output: str, error: str | None, denied: bool = False
    ) -> None:
        if denied:
            tool_call.status = ToolCallStatus.DENIED
        else:
            tool_call.status = ToolCallStatus.SUCCEEDED if ok else ToolCallStatus.FAILED
        tool_call.result = {"output": output}
        tool_call.error = error
        tool_call.finished_at = utcnow()
        await self._db.commit()

    async def create_approval(
        self, run: Run, *, action_type: str, payload: dict[str, Any]
    ) -> Approval:
        """ApprovalRequest: payload содержит фактическую команду/аргументы (§12)."""
        approval = Approval(run_id=run.id, action_type=action_type, payload=payload)
        self._db.add(approval)
        await self._db.commit()
        return approval

    async def find_approval_for_call(self, run: Run, call_id: str) -> Approval | None:
        """Approval для конкретного tool call (payload.call_id) — любой статус."""
        result = await self._db.execute(select(Approval).where(Approval.run_id == run.id))
        for approval in result.scalars():
            if approval.payload.get("call_id") == call_id:
                return approval
        return None

    async def fetch_pending_approvals(self, limit: int = 50) -> list[Approval]:
        result = await self._db.execute(
            select(Approval)
            .where(Approval.status == ApprovalStatus.PENDING)
            .order_by(Approval.created_at)
            .limit(limit)
        )
        return list(result.scalars())

    async def find_approval_by_prefix(self, approval_id: str) -> Approval:
        result = await self._db.execute(select(Approval).where(Approval.id.startswith(approval_id)))
        approvals = list(result.scalars())
        if not approvals:
            raise ApprovalNotFoundError(approval_id)
        if len(approvals) > 1:
            raise ApprovalNotFoundError(f"{approval_id} (префикс неоднозначен: {len(approvals)})")
        return approvals[0]

    async def decide_approval(
        self, approval: Approval, *, approved: bool, decided_by: str, reason: str | None = None
    ) -> None:
        """ApprovalDecision: зафиксировать решение человека (§15)."""
        approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
        approval.decided_at = utcnow()
        approval.decided_by = decided_by
        approval.reason = reason
        await self._db.commit()

    async def answer_question(self, approval: Approval, *, answer: str, answered_by: str) -> None:
        """ask_user: зафиксировать текстовый ответ человека (§6.5).

        Ответ хранится в reason (APPROVED = отвечено); пустой ответ — согласие
        продолжить без уточнения.
        """
        approval.status = ApprovalStatus.APPROVED
        approval.decided_at = utcnow()
        approval.decided_by = answered_by
        approval.reason = answer
        await self._db.commit()

    async def latest_approval(self, run_id: str) -> Approval | None:
        """Последний approval run'а — resume внешнего агента строит по нему
        prompt-решение (ADR-0016 §7)."""
        result = await self._db.execute(
            select(Approval)
            .where(Approval.run_id == run_id)
            .order_by(Approval.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def last_agent_session(self, session_id: str) -> str | None:
        """agent_session_id последнего run'а Session — chat-непрерывность
        внешнего агента (ADR-0016 фаза 3)."""
        result = await self._db.execute(
            select(Run).where(Run.session_id == session_id).order_by(Run.created_at.desc())
        )
        for run in result.scalars():
            value = (run.meta or {}).get("agent_session_id")
            if isinstance(value, str) and value:
                return value
        return None

    async def expire_approval(self, approval: Approval) -> None:
        """Пометить вопрос/approval истёкшим по таймауту (§6.5)."""
        approval.status = ApprovalStatus.EXPIRED
        approval.decided_at = utcnow()
        approval.decided_by = "timeout"
        await self._db.commit()

    async def enqueue_memory_change(self, run: Run, change: dict[str, Any]) -> MemoryChange:
        """Поставить MemoryChangeRequest в очередь single writer'а (ADR-0004)."""
        row = MemoryChange(change=change, source_run_id=run.id)
        self._db.add(row)
        await self._db.commit()
        return row

    async def log_skill_load(
        self, run: Run, *, skill_name: str, skill_version: str | None, source: str = "full"
    ) -> None:
        """Факт загрузки скилла — сырьё для Skill Curator (ADR-0009, §18.1)."""
        self._db.add(
            SkillLoad(
                run_id=run.id,
                skill_name=skill_name,
                skill_version=skill_version,
                source=source,
            )
        )
        await self._db.commit()

    async def loaded_skill_names(self, run: Run) -> set[str]:
        """Имена скиллов, загруженных в run (для skill-specific checks)."""
        result = await self._db.execute(
            select(SkillLoad.skill_name).where(SkillLoad.run_id == run.id)
        )
        return set(result.scalars())

    async def log_check_result(
        self, run: Run, *, name: str, status: CheckStatus, output: str
    ) -> None:
        """Результат детерминированной проверки verifier'а (§6.11)."""
        self._db.add(CheckResult(run_id=run.id, check_name=name, status=status, output=output))
        await self._db.commit()

    async def find_completed_child_run(self, parent_run_id: str, task: str) -> Run | None:
        """Завершённый дочерний run родителя с той же задачей (ADR-0015 фаза 3).

        Идемпотентность spawn_child_run на границе write-ahead: если родитель
        упал между исполнением ребёнка и checkpoint'ом, resume переисполняет
        вызов — результат берётся из trace, ребёнок не гоняется повторно.
        """
        result = await self._db.execute(
            select(Run)
            .where(
                Run.parent_run_id == parent_run_id,
                Run.task == task,
                Run.state == RunState.COMPLETED,
            )
            .order_by(Run.created_at.desc())
        )
        return result.scalars().first()

    async def last_assistant_text(self, run: Run) -> str:
        """Финальный ответ run'а из trace: последний непустой assistant-текст."""
        result = await self._db.execute(
            select(Message)
            .where(Message.run_id == run.id, Message.role == "assistant")
            .order_by(Message.index_in_run.desc())
        )
        for message in result.scalars():
            text = str(message.content.get("content") or "")
            if text.strip():
                return text
        return ""

    async def session_history(
        self, session_id: str, *, limit_messages: int = 24
    ) -> list[dict[str, str]]:
        """Диалог сессии из trace для продолжения/форка chat (ADR-0015 фаза 5).

        По каждому run сессии — пара (user=task, assistant=финальный ответ):
        ровно то, что chat накапливает в history по ходу живой сессии.
        Возвращает [{"role", "content"}, ...] — recorder не зависит от llm-слоя.
        """
        runs = (
            (
                await self._db.execute(
                    select(Run).where(Run.session_id == session_id).order_by(Run.created_at)
                )
            )
            .scalars()
            .all()
        )
        history: list[dict[str, str]] = []
        for run in runs:
            answer = await self.last_assistant_text(run)
            history.append({"role": "user", "content": run.task})
            history.append({"role": "assistant", "content": answer or "(без ответа)"})
        return history[-limit_messages:]

    async def rename_session(self, session: Session, title: str) -> None:
        session.title = title
        await self._db.commit()

    async def get_run(self, run_id: str) -> Run | None:
        return await self._db.get(Run, run_id)

    async def failed_check_count(self, run_id: str) -> int:
        """Число непрошедших проверок run'а (FAILED/ERROR) — для exit-кода."""
        result = await self._db.execute(
            select(func.count())
            .select_from(CheckResult)
            .where(
                CheckResult.run_id == run_id,
                CheckResult.status.in_([CheckStatus.FAILED, CheckStatus.ERROR]),
            )
        )
        return int(result.scalar_one())

    async def acquire_workspace_lease(self, workspace: str) -> None:
        """Отказать в старте, если на workspace уже есть живой RUNNING run.

        Живой = RUNNING с heartbeat моложе порога. Протухшие (упавший процесс)
        не блокируют — их подберёт recovery. Single-writer у workspace: gateway
        не запускает второй run на залоченном рабочем дереве (ADR-0015 §0.5).
        """
        cutoff = utcnow() - timedelta(seconds=_HEARTBEAT_STALE_SEC)
        result = await self._db.execute(
            select(Run).where(Run.state == RunState.RUNNING, Run.workspace == workspace)
        )
        for run in result.scalars():
            if run.heartbeat_at is not None and run.heartbeat_at >= cutoff:
                raise WorkspaceBusyError(
                    f"workspace занят активным run {run.id[:8]} "
                    f"(heartbeat {run.heartbeat_at.isoformat()}): дождитесь его завершения "
                    f"или приостановите (ADR-0015 §0.5)"
                )

    async def update_progress(
        self,
        run: Run,
        *,
        iterations: int,
        tokens_used: int,
        cost_usd: float,
        cached_tokens: int = 0,
    ) -> None:
        run.iterations = iterations
        run.tokens_used = tokens_used
        run.cost_usd = cost_usd
        run.heartbeat_at = utcnow()  # lease heartbeat (ADR-0015 §0.5)
        if cached_tokens:
            # JSON-колонка отслеживает только переприсваивание.
            run.meta = {**run.meta, "cached_tokens": cached_tokens}
        await self._db.commit()

    async def merge_run_meta(self, run: Run, extra: dict[str, object]) -> None:
        """Дописать ключи в Run.meta (executor/adapter/agent_session_id, ADR-0016).

        JSON-колонка отслеживает только переприсваивание — мутировать словарь
        на месте нельзя. Перед слиянием обновляем run из БД: без этого
        локальный (устаревший) meta затёр бы флаг, выставленный параллельно
        другой сессией (например, cancel_requested из cooperative-cancel,
        ADR-0017 §2) — сама фаза (блок A §5) пишется в этом же месте на
        каждой итерации, так что окно гонки было бы открыто постоянно.
        """
        await self._db.refresh(run)
        run.meta = {**run.meta, **extra}
        await self._db.commit()

    async def finish_run(self, run: Run, state: RunState, *, error: str | None = None) -> None:
        run.state = state
        run.error = error
        run.finished_at = utcnow()
        await self._db.commit()

    async def request_cancel(self, run: Run) -> None:
        """Пометить run к cooperative-cancel (ADR-0017 §2).

        Живую ногу loop не прерываем посреди tool-исполнения: флаг читается
        loop'ом на границе итерации, checkpoint сохраняется, run уходит в
        CANCELLED.
        """
        await self.merge_run_meta(run, {CANCEL_META_KEY: True})

    async def cancel_requested(self, run: Run) -> bool:
        """Прочитать флаг cancel из БД (пишется другим процессом/сессией)."""
        await self._db.refresh(run)
        return bool((run.meta or {}).get(CANCEL_META_KEY))

    async def create_session(self, *, title: str, meta: dict[str, Any] | None = None) -> Session:
        """Сессия без run'а — для gateway-chat (ADR-0017 §2): workspace сессии
        фиксируется в meta до первого сообщения."""
        session = Session(title=title[:200], meta=meta or {})
        self._db.add(session)
        await self._db.commit()
        return session

    async def set_run_state(self, run: Run, state: RunState, *, error: str | None = None) -> None:
        """Нетерминальный переход state machine (suspended, waiting_approval,
        running при resume): finished_at не выставляется."""
        run.state = state
        run.error = error
        await self._db.commit()

    async def save_checkpoint(self, run: Run, *, iteration: int, state: dict[str, Any]) -> None:
        """Checkpoint после каждого шага loop (ADR-0005, write-ahead)."""
        self._db.add(Checkpoint(run_id=run.id, iteration=iteration, state=state))
        await self._db.commit()

    async def load_resumable(self, run_id_prefix: str) -> tuple[Run, dict[str, Any]]:
        """Найти run для resume и состояние его последнего checkpoint."""
        run = await find_run_by_prefix(self._db, run_id_prefix)
        if run.state not in _RESUMABLE_STATES:
            raise RunNotResumableError(
                f"run {run.id[:8]} в состоянии '{run.state.value}' — "
                f"возобновить можно только suspended/waiting_approval"
            )
        result = await self._db.execute(
            select(Checkpoint)
            .where(Checkpoint.run_id == run.id)
            .order_by(Checkpoint.iteration.desc(), Checkpoint.created_at.desc())
            .limit(1)
        )
        checkpoint = result.scalar_one_or_none()
        if checkpoint is None:
            raise RunNotResumableError(
                f"у run {run.id[:8]} нет checkpoint — возобновление невозможно"
            )
        return run, checkpoint.state

    async def find_refuel_suspended_runs(self, limit: int = 50) -> list[Run]:
        """Suspended runs, приостановленные refuel (§6.10) — для авто-супервизора.

        Refuel-приостановку отличаем по последнему checkpoint'у (`refuel_pending`),
        а не по тексту ошибки: budget/max/crash-suspend его не выставляют, поэтому
        супервизор не трогает остановки, требующие человека.
        """
        result = await self._db.execute(
            select(Run).where(Run.state == RunState.SUSPENDED).order_by(Run.started_at).limit(limit)
        )
        refuel_suspended: list[Run] = []
        for run in result.scalars():
            checkpoint = await self._db.execute(
                select(Checkpoint)
                .where(Checkpoint.run_id == run.id)
                .order_by(Checkpoint.iteration.desc(), Checkpoint.created_at.desc())
                .limit(1)
            )
            latest = checkpoint.scalar_one_or_none()
            if latest is not None and latest.state.get("refuel_pending"):
                refuel_suspended.append(run)
        return refuel_suspended

    async def recover_interrupted_runs(self) -> list[Run]:
        """Runs, оставшиеся в running после падения процесса, → suspended.

        Опирается на протухший heartbeat, а не на голое состояние RUNNING
        (ADR-0015 §0.5): run с живым heartbeat исполняется в другом процессе и
        НЕ приостанавливается ложно; приостанавливаются только те, чей процесс
        мёртв (heartbeat старше порога или отсутствует). Так снимается принятый
        ранее компромисс с ложной приостановкой чужого активного run'а.
        """
        cutoff = utcnow() - timedelta(seconds=_HEARTBEAT_STALE_SEC)
        result = await self._db.execute(select(Run).where(Run.state == RunState.RUNNING))
        interrupted: list[Run] = []
        for run in result.scalars():
            if run.heartbeat_at is not None and run.heartbeat_at >= cutoff:
                continue  # живой run в другом процессе — не трогаем
            run.state = RunState.SUSPENDED
            run.error = "процесс был прерван — run приостановлен recovery при старте"
            interrupted.append(run)
        if interrupted:
            await self._db.commit()
        return interrupted
