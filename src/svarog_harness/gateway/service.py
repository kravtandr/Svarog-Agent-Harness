"""GatewayService: оркестрация runs для внешних интерфейсов (§6.1, §10.4).

Gateway не содержит логики агента — он запускает `TaskRunner` в фоновой
asyncio-задаче, отдаёт клиенту run_id сразу после старта run и стримит
события через `EventStream`. Approval асинхронный (ADR-0005): run уходит в
`waiting_approval`, решение приходит позже любым интерфейсом и возобновляет
run в фоне. Источник истины по trace — SQLite; события — «живой» слой.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.paths import skills_dirs
from svarog_harness.config.schema import AutonomyMode, SvarogConfig
from svarog_harness.gateway.models import (
    ApprovalView,
    RunDetail,
    RunSummary,
    SkillCard,
    ToolCallView,
)
from svarog_harness.runtime.loop import RunOutcome
from svarog_harness.runtime.orchestrator import RunHooks, TaskRunner
from svarog_harness.skills import scan_skills
from svarog_harness.storage.events import EventStream, InProcessEventStream
from svarog_harness.storage.models import Run
from svarog_harness.trace.lookup import ApprovalNotFoundError, RunNotFoundError
from svarog_harness.trace.recorder import TraceRecorder
from svarog_harness.trace.viewer import fetch_run, fetch_runs
from svarog_harness.verifier import CheckOutcome


@dataclass
class _RunHolder:
    """Мутабельный держатель run_id: on_run_started заполняет его до прочих хуков."""

    run_id: str | None = None


@dataclass
class GatewayService:
    cfg: SvarogConfig
    workspace: Path
    events: EventStream = field(default_factory=InProcessEventStream)

    def __post_init__(self) -> None:
        self._runner = TaskRunner(self.cfg, self.workspace)
        # Держим ссылки на фоновые задачи, чтобы их не собрал GC (RUF006).
        self._tasks: set[asyncio.Task[None]] = set()

    # --- запуск и возобновление runs -------------------------------------

    async def create_run(self, task: str, autonomy: AutonomyMode | None) -> str:
        """Запустить run в фоне; вернуть run_id, как только он создан."""
        mode = autonomy if autonomy is not None else self.cfg.runtime.autonomy
        started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._spawn(self._run_bg(task, mode, started))
        return await started

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_bg(
        self, task: str, autonomy: AutonomyMode, started: asyncio.Future[str]
    ) -> None:
        holder = _RunHolder()
        hooks = self._event_hooks(holder, started)
        try:
            outcome = await self._runner.run_once(task, autonomy, hooks=hooks)
            self._publish_finished(outcome)
        except Exception as exc:
            self._publish_error(holder, started, exc)

    async def resume_run(self, run_id: str) -> None:
        """Возобновить run в фоне (после решения approval / из suspended)."""
        # Новая нога стримит с чистого листа: старый run_finished не должен
        # обрывать подписчика, подключившегося после возобновления.
        self.events.reset(run_id)
        self._spawn(self._resume_bg(run_id))

    async def _resume_bg(self, run_id: str) -> None:
        holder = _RunHolder(run_id=run_id)
        started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        started.set_result(run_id)
        hooks = self._event_hooks(holder, started)
        try:
            outcome = await self._runner.resume(run_id, hooks=hooks)
            self._publish_finished(outcome)
        except Exception as exc:
            self._publish_error(holder, started, exc)

    # --- события ----------------------------------------------------------

    def _event_hooks(self, holder: _RunHolder, started: asyncio.Future[str]) -> RunHooks:
        def on_started(run: Run) -> None:
            holder.run_id = run.id
            if not started.done():
                started.set_result(run.id)

        def emit(event: dict[str, Any]) -> None:
            if holder.run_id is not None:
                self.events.publish(holder.run_id, event)

        def on_check(check: CheckOutcome) -> None:
            emit({"type": "check", "name": check.name, "status": check.status.value})

        return RunHooks(
            on_run_started=on_started,
            on_text_delta=lambda delta: emit({"type": "text", "delta": delta}),
            on_tool_call=lambda name, args: emit({"type": "tool_call", "tool": name}),
            on_notify=lambda name, reason: emit({"type": "notify", "tool": name, "reason": reason}),
            on_check=on_check,
            on_commit=lambda sha, branch, push: emit(
                {"type": "commit", "sha": sha, "branch": branch}
            ),
        )

    def _publish_finished(self, outcome: RunOutcome) -> None:
        self.events.publish(
            outcome.run_id,
            {
                "type": "run_finished",
                "run_id": outcome.run_id,
                "state": outcome.state.value,
                "final_answer": outcome.final_answer,
                "error": outcome.error,
            },
        )

    def _publish_error(
        self, holder: _RunHolder, started: asyncio.Future[str], exc: Exception
    ) -> None:
        if not started.done():
            started.set_exception(exc)
            return
        if holder.run_id is not None:
            self.events.publish(
                holder.run_id,
                {
                    "type": "run_finished",
                    "state": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )

    def stream(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        """Асинхронный итератор событий run'а (история + живые)."""
        return self.events.stream(run_id)

    # --- чтение trace -----------------------------------------------------

    async def _read[T](self, action: Callable[[AsyncSession], Awaitable[T]]) -> T:
        return await self._runner.with_db(action)

    async def list_runs(self, limit: int = 20) -> list[RunSummary]:
        async def action(db: AsyncSession) -> list[RunSummary]:
            return [_summary(run) for run in await fetch_runs(db, limit=limit)]

        return await self._read(action)

    async def get_run(self, run_id: str) -> RunDetail:
        async def action(db: AsyncSession) -> RunDetail:
            run, messages, tool_calls, checks = await fetch_run(db, run_id)
            return RunDetail(
                **_summary(run).model_dump(),
                messages=[{"role": m.role, "index": m.index_in_run, **m.content} for m in messages],
                tool_calls=[
                    ToolCallView(
                        tool_name=c.tool_name,
                        risk_level=c.risk_level,
                        policy_decision=c.policy_decision,
                        status=c.status.value,
                        error=c.error,
                    )
                    for c in tool_calls
                ],
                checks=[{"name": c.check_name, "status": c.status.value} for c in checks],
            )

        return await self._read(action)

    async def list_pending_approvals(self) -> list[ApprovalView]:
        async def action(db: AsyncSession) -> list[ApprovalView]:
            approvals = await TraceRecorder(db).fetch_pending_approvals()
            return [
                ApprovalView(
                    approval_id=a.id,
                    run_id=a.run_id,
                    action_type=a.action_type,
                    payload=a.payload or {},
                )
                for a in approvals
            ]

        return await self._read(action)

    async def decide_approval(self, approval_id: str, *, approved: bool, reason: str | None) -> str:
        """Записать решение человека; вернуть run_id для возобновления (ADR-0005)."""

        async def action(db: AsyncSession) -> str:
            recorder = TraceRecorder(db)
            approval = await recorder.find_approval_by_prefix(approval_id)
            await recorder.decide_approval(
                approval, approved=approved, decided_by="api", reason=reason
            )
            return approval.run_id

        return await self._read(action)

    def list_skills(self) -> list[SkillCard]:
        scan = scan_skills(skills_dirs(self.cfg, self.workspace))
        return [
            SkillCard(
                name=s.name,
                description=s.metadata.description,
                version=s.metadata.version,
                risk=s.metadata.risk.value,
            )
            for s in scan.skills
        ]


def _summary(run: Run) -> RunSummary:
    return RunSummary(
        run_id=run.id,
        state=run.state.value,
        task=run.task,
        autonomy=run.autonomy,
        iterations=run.iterations,
        tokens_used=run.tokens_used,
        cost_usd=run.cost_usd,
        error=run.error,
    )


__all__ = [
    "ApprovalNotFoundError",
    "GatewayService",
    "RunNotFoundError",
]
