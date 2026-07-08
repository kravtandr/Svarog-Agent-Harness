"""Запись trace в storage (§6.12, §15): сообщения, tool calls, прогресс run'а.

Recorder — единственное место, где agent loop пишет в БД; сам loop
не знает про SQLAlchemy-модели напрямую.
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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


class TraceRecorder:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._message_index: dict[str, int] = {}

    async def start_run(
        self, *, task: str, autonomy: str, model: str, session_id: str | None = None
    ) -> Run:
        # chat переиспользует одну Session на серию runs (§10.1, ADR-0008).
        if session_id is None:
            session = Session(title=task[:200])
            self._db.add(session)
            run = Run(
                session=session,
                state=RunState.RUNNING,
                task=task,
                autonomy=autonomy,
                started_at=utcnow(),
                meta={"model": model},
            )
        else:
            run = Run(
                session_id=session_id,
                state=RunState.RUNNING,
                task=task,
                autonomy=autonomy,
                started_at=utcnow(),
                meta={"model": model},
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

    async def update_progress(
        self, run: Run, *, iterations: int, tokens_used: int, cost_usd: float
    ) -> None:
        run.iterations = iterations
        run.tokens_used = tokens_used
        run.cost_usd = cost_usd
        await self._db.commit()

    async def finish_run(self, run: Run, state: RunState, *, error: str | None = None) -> None:
        run.state = state
        run.error = error
        run.finished_at = utcnow()
        await self._db.commit()

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

    async def recover_interrupted_runs(self) -> list[Run]:
        """Runs, оставшиеся в running после падения процесса, → suspended.

        Вызывается при старте CLI-команд, работающих с runs. В однопользовательском
        CLI (ADR-0008) параллельный процесс с активным run — вырожденный случай;
        ложная приостановка чужого активного run'а здесь возможна и осознанно
        принята до server-режимов.
        """
        result = await self._db.execute(select(Run).where(Run.state == RunState.RUNNING))
        interrupted = list(result.scalars())
        for run in interrupted:
            run.state = RunState.SUSPENDED
            run.error = "процесс был прерван — run приостановлен recovery при старте"
        if interrupted:
            await self._db.commit()
        return interrupted
