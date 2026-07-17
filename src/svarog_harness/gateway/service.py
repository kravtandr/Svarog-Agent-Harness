"""GatewayService: оркестрация runs для внешних интерфейсов (§6.1, §10.4).

Gateway не содержит логики агента — он запускает `TaskRunner` в фоновой
asyncio-задаче, отдаёт клиенту run_id сразу после старта run и стримит
события через `EventStream`. Approval асинхронный (ADR-0005): run уходит в
`waiting_approval`, решение приходит позже любым интерфейсом и возобновляет
run в фоне. Источник истины по trace — SQLite; события — «живой» слой.
"""

import asyncio
import contextlib
import os
import tarfile
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.paths import skills_dirs
from svarog_harness.config.schema import AutonomyMode, SvarogConfig, TenantRole
from svarog_harness.gateway.models import (
    ApprovalView,
    CancelView,
    RepoSpec,
    RunDetail,
    RunDiffView,
    RunSummary,
    SessionView,
    SkillCard,
    ToolCallView,
    WhoamiView,
    WorkspaceView,
)
from svarog_harness.gitflow.provision import (
    DEFAULT_GIT_CREDENTIALS_REF,
    CloneError,
    UnknownWorkspaceError,
    create_named_workspace,
    delete_named_workspace,
    list_named_workspaces,
    provision_clone,
    resolve_named_workspace,
    resolve_workspace_file,
    sweep_task_workspaces,
    task_workspace_dir,
)
from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.llm.provider import ChatMessage
from svarog_harness.runtime.loop import RunOutcome
from svarog_harness.runtime.orchestrator import RunHooks, SessionResources, TaskRunner
from svarog_harness.skills import scan_skills
from svarog_harness.storage.events import EventStream, InProcessEventStream
from svarog_harness.storage.models import Run, RunState, Session
from svarog_harness.tenant.quota import QuotaUsage
from svarog_harness.trace.lookup import (
    ApprovalNotFoundError,
    RunNotFoundError,
    find_run_by_prefix,
    find_session_by_prefix,
)
from svarog_harness.trace.recorder import TraceRecorder, WorkspaceBusyError
from svarog_harness.trace.viewer import fetch_run, fetch_runs, run_usage_totals
from svarog_harness.verifier import CheckOutcome

# Диф от корня истории, когда первый коммит run'а — root commit.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
# Retention-GC task-workspace'ов гоняется не чаще раза в час (ADR-0017).
_GC_INTERVAL_SEC = 3600.0
# Незавершённые состояния: их workspace GC не трогает (resume должен работать).
_LIVE_STATES = (RunState.PENDING, RunState.RUNNING, RunState.WAITING_APPROVAL, RunState.SUSPENDED)
# Терминальные состояния: cancel к ним неприменим (409).
_TERMINAL_STATES = (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED)
# Сообщений истории сессии в контексте run'а (как _CHAT_HISTORY_LIMIT в CLI-chat).
_SESSION_HISTORY_LIMIT = 24


class CancelNotAllowedError(Exception):
    """Run уже терминален — отменять нечего (ADR-0017 §2)."""


@dataclass
class _WarmSlot:
    """Тёплый sandbox сессии: env/infra/MCP живут между сообщениями (ADR-0017).

    Runner хранится вместе с ресурсами: env смонтирован на его workspace,
    и все сообщения сессии обязаны идти через один и тот же runner.
    """

    workspace: Path
    runner: TaskRunner
    resources: SessionResources
    last_used: float


@dataclass
class _RunHolder:
    """Мутабельный держатель run_id: on_run_started заполняет его до прочих хуков."""

    run_id: str | None = None


@dataclass
class GatewayService:
    cfg: SvarogConfig
    workspace: Path
    events: EventStream = field(default_factory=InProcessEventStream)
    # Колбэк на создание run'а — TenantHub пишет им run_index run→tenant (ADR-0014).
    on_run_created: Callable[[str], None] | None = None
    # Роль тенанта (ADR-0013): фиксируется в runner'е и держит кламп на resume.
    role: TenantRole = TenantRole.SUPERUSER
    # Проверка квоты перед стартом run'а — TenantHub вешает сюда лимиты тенанта
    # (ADR-0014, Фаза 3); бросает QuotaExceededError. None — без квот.
    quota_guard: Callable[[], Awaitable[None]] | None = None
    # Идентичность для /whoami (ADR-0017 §2); TenantHub проставляет tenant_id.
    tenant_id: str = "local"

    def __post_init__(self) -> None:
        self._runner = TaskRunner(self.cfg, self.workspace, role=self.role)
        # Держим ссылки на фоновые задачи, чтобы их не собрал GC (RUF006).
        self._tasks: set[asyncio.Task[None]] = set()
        # Супервизор refuel (§6.10): счётчик авто-resume'ов на run (предохранитель)
        # и множество run'ов с уже запущенным авто-возобновлением (без гонки).
        self._auto_resumes: dict[str, int] = {}
        self._inflight: set[str] = set()
        # Retention-GC task-workspace'ов (ADR-0017): троттлинг по monotonic.
        self._last_gc = 0.0
        # Тёплые sandbox'ы сессий gateway-chat: session_id → слот; создание
        # сериализовано локом (двойной слот = утёкший контейнер).
        self._warm: dict[str, _WarmSlot] = {}
        self._warm_lock = asyncio.Lock()

    # --- per-run workspaces (ADR-0017) ------------------------------------

    def _runner_for(self, workspace: Path) -> TaskRunner:
        """Runner для workspace run'а; workspace сервиса — общий self._runner.

        Per-run runner делит с сервисом конфиг (та же БД/память/секреты
        тенанта) и отличается только рабочим деревом — изоляция путей ядра
        уже параметризована по workspace (ADR-0012).
        """
        ws = workspace.expanduser().resolve()
        if ws == self.workspace.expanduser().resolve():
            return self._runner
        return TaskRunner(self.cfg, ws, role=self.role)

    async def _provision_workspace(
        self, task: str, repo: RepoSpec | None, name: str | None
    ) -> Path:
        """Workspace будущего run'а: named / git-клон / workspace сервиса."""
        if name is not None:
            path = resolve_named_workspace(self.workspace, name).resolve()
            # Ранний отказ 409 до docker/LLM; авторитетный lease-гард всё равно
            # срабатывает в run_once (ADR-0015 §0.5) — тут только быстрый UX.
            if await self._workspace_busy(path):
                raise WorkspaceBusyError(f"workspace '{name}' занят активным run")
            return path
        if repo is not None:
            dest = task_workspace_dir(self.workspace, task)
            credentials = self._git_credentials(repo.credentials_ref)
            await provision_clone(repo.url, dest, ref=repo.ref, credentials=credentials)
            return dest.resolve()
        return self.workspace

    def _git_credentials(self, ref: str | None) -> str | None:
        """Git-credentials из tenant-store (ADR-0017 развилка 3), только host-side.

        Явно названный ref обязан существовать; конвенциональный
        "git.credentials" опционален (нет секрета — анонимный clone).
        """
        store = self._runner.store  # tenant-скоуп (для standard — без env-fallback)
        if ref is not None:
            value = store.get(ref)
            if not value:
                raise CloneError(f"секрет '{ref}' (credentials_ref) не найден в tenant-store")
            return value
        return store.get(DEFAULT_GIT_CREDENTIALS_REF) or None

    async def _workspace_busy(self, path: Path) -> bool:
        """Есть ли живой run в workspace (lease-семантика ADR-0015 §0.5)."""

        async def action(db: AsyncSession) -> bool:
            try:
                await TraceRecorder(db).acquire_workspace_lease(str(path))
            except WorkspaceBusyError:
                return True
            return False

        return await self._read(action)

    # --- тёплые sandbox'ы сессий (ADR-0017) --------------------------------

    async def _acquire_warm(
        self, session_id: str, workspace: Path, autonomy: AutonomyMode
    ) -> _WarmSlot | None:
        """Слот тёплого sandbox сессии; None — фича выключена (ttl=0).

        Первый вызов сессии поднимает env/infra/MCP один раз; дальнейшие
        сообщения переиспользуют их, экономя старт контейнера (~1.5-3s).
        """
        if self.cfg.cloud.warm_session_ttl_sec <= 0:
            return None
        async with self._warm_lock:
            slot = self._warm.get(session_id)
            if slot is not None:
                slot.last_used = time.monotonic()
                return slot
            runner = self._runner_for(workspace)
            resources = await runner.prepare_session_resources(autonomy)
            slot = _WarmSlot(
                workspace=workspace,
                runner=runner,
                resources=resources,
                last_used=time.monotonic(),
            )
            self._warm[session_id] = slot
            return slot

    async def _drop_warm(self, session_id: str) -> None:
        """Закрыть и забыть тёплый слот (ошибка ноги / TTL / shutdown)."""
        slot = self._warm.pop(session_id, None)
        if slot is not None:
            await slot.resources.close()

    async def close_warm_sessions(self) -> None:
        """Закрыть все тёплые sandbox'ы (graceful shutdown, тесты)."""
        for session_id in list(self._warm):
            await self._drop_warm(session_id)

    async def _sweep_warm_sessions(self) -> None:
        """Закрыть тёплые слоты, простоявшие дольше TTL (idle-GC).

        Слот с живым run'ом (lease workspace) не трогаем: длинный run — не
        простой; его last_used обновится при следующем сообщении.
        """
        ttl = float(self.cfg.cloud.warm_session_ttl_sec)
        if ttl <= 0:
            return
        now = time.monotonic()
        for session_id, slot in list(self._warm.items()):
            if now - slot.last_used < ttl:
                continue
            if await self._workspace_busy(slot.workspace):
                continue
            await self._drop_warm(session_id)

    # --- запуск и возобновление runs -------------------------------------

    async def usage(self) -> QuotaUsage:
        """Снимок использования по БД тенанта (для квот, ADR-0014 Фаза 3)."""

        async def action(db: AsyncSession) -> QuotaUsage:
            active, cost, tokens = await run_usage_totals(db)
            return QuotaUsage(active_runs=active, total_cost_usd=cost, total_tokens=tokens)

        return await self._read(action)

    async def create_run(
        self,
        task: str,
        autonomy: AutonomyMode | None,
        *,
        repo: RepoSpec | None = None,
        workspace_name: str | None = None,
    ) -> str:
        """Запустить run в фоне; вернуть run_id, как только он создан.

        Источник workspace (ADR-0017): git-клон в одноразовый task-workspace
        (`repo`), постоянный named workspace (`workspace_name`) либо workspace
        сервиса. Квота проверяется ДО клона (429 раньше сетевой работы).
        """
        if self.quota_guard is not None:
            await self.quota_guard()  # QuotaExceededError → 429 на транспорте
        workspace = await self._provision_workspace(task, repo, workspace_name)
        mode = autonomy if autonomy is not None else self.cfg.runtime.autonomy
        started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._spawn(self._run_bg(task, mode, started, runner=self._runner_for(workspace)))
        return await started

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_bg(
        self,
        task: str,
        autonomy: AutonomyMode,
        started: asyncio.Future[str],
        *,
        runner: TaskRunner | None = None,
        session_id: str | None = None,
        history: list[ChatMessage] | None = None,
        warm: _WarmSlot | None = None,
    ) -> None:
        holder = _RunHolder()
        hooks = self._event_hooks(holder, started)
        try:
            outcome = await (runner or self._runner).run_once(
                task,
                autonomy,
                hooks=hooks,
                session_id=session_id,
                history=history,
                resources=warm.resources if warm is not None else None,
            )
            self._publish_finished(outcome)
        except Exception as exc:
            if warm is not None and session_id is not None:
                # Нога упала — sandbox может быть в неизвестном состоянии
                # (умерший контейнер и т.п.); следующий message поднимет свежий.
                await self._drop_warm(session_id)
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
            # Runner — по workspace run'а (per-run workspaces, ADR-0017):
            # resume под конфигом тенанта, а не сервиса-по-умолчанию.
            runner = await self._runner_for_run(run_id)
            outcome = await runner.resume(run_id, hooks=hooks)
            self._publish_finished(outcome)
        except Exception as exc:
            self._publish_error(holder, started, exc)

    async def _runner_for_run(self, run_id: str) -> TaskRunner:
        """Runner, привязанный к workspace существующего run'а (для resume)."""

        async def action(db: AsyncSession) -> str | None:
            run = await find_run_by_prefix(db, run_id)
            return run.workspace

        workspace = await self._read(action)
        if not workspace:
            return self._runner
        return self._runner_for(Path(workspace))

    # --- события ----------------------------------------------------------

    def _event_hooks(self, holder: _RunHolder, started: asyncio.Future[str]) -> RunHooks:
        def on_started(run: Run) -> None:
            holder.run_id = run.id
            # run_index run→tenant (ADR-0014): идемпотентно, безопасно и на resume.
            if self.on_run_created is not None:
                self.on_run_created(run.id)
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

    async def wait_for_background(self) -> None:
        """Дождаться завершения фоновых run/resume-задач (graceful shutdown, тесты)."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

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

    async def answer_question(self, approval_id: str, *, answer: str) -> str:
        """Записать текстовый ответ на ask_user; вернуть run_id (§6.5)."""

        async def action(db: AsyncSession) -> str:
            recorder = TraceRecorder(db)
            approval = await recorder.find_approval_by_prefix(approval_id)
            await recorder.answer_question(approval, answer=answer, answered_by="api")
            return approval.run_id

        return await self._read(action)

    async def cancel_run(self, run_id: str) -> CancelView:
        """Cooperative-cancel (ADR-0017 §2).

        Run без живой ноги (waiting_approval/suspended/протухший) терминализируется
        сразу — его pending-approvals закрываются отказом. Живой RUNNING получает
        флаг в meta: loop завершит run на границе итерации, checkpoint сохранён.
        """

        async def action(db: AsyncSession) -> CancelView:
            recorder = TraceRecorder(db)
            run = await find_run_by_prefix(db, run_id)
            if run.state in _TERMINAL_STATES:
                raise CancelNotAllowedError(
                    f"run {run.id[:8]} уже в терминальном состоянии '{run.state.value}'"
                )
            if run.state == RunState.RUNNING:
                await recorder.request_cancel(run)
                return CancelView(run_id=run.id, state="cancelling")
            # waiting_approval / suspended / pending: живой ноги нет —
            # терминализируем сразу и закрываем pending-approvals отказом.
            for approval in await recorder.fetch_pending_approvals():
                if approval.run_id == run.id:
                    await recorder.decide_approval(
                        approval, approved=False, decided_by="cancel", reason="run отменён"
                    )
            await recorder.finish_run(run, RunState.CANCELLED)
            return CancelView(run_id=run.id, state="cancelled")

        view = await self._read(action)
        if view.state == "cancelled":
            self.events.publish(
                view.run_id,
                {"type": "run_finished", "run_id": view.run_id, "state": "cancelled"},
            )
        return view

    async def whoami(self) -> WhoamiView:
        """Идентичность и usage тенанта (ADR-0017 §2)."""
        usage = await self.usage()
        return WhoamiView(
            tenant_id=self.tenant_id,
            role=self.role.value,
            active_runs=usage.active_runs,
            total_cost_usd=usage.total_cost_usd,
            total_tokens=usage.total_tokens,
        )

    # --- сессии gateway-chat (ADR-0017 §2, семантика §10.1) ---------------

    async def create_session(
        self,
        *,
        title: str = "",
        repo: RepoSpec | None = None,
        workspace_name: str | None = None,
    ) -> SessionView:
        """Сессия: workspace провижнится один раз и живёт всю серию runs."""
        workspace = await self._provision_workspace(title or "session", repo, workspace_name)
        meta = {"workspace": str(workspace.expanduser().resolve())}

        async def action(db: AsyncSession) -> Session:
            return await TraceRecorder(db).create_session(
                title=title or "gateway-сессия", meta=meta
            )

        session = await self._read(action)
        return SessionView(
            session_id=session.id, title=session.title or "", workspace=meta["workspace"], runs=[]
        )

    async def send_message(self, session_id: str, text: str, autonomy: AutonomyMode | None) -> str:
        """Сообщение чата → отдельный run в workspace сессии с её историей.

        Контекст диалога — по типу executor'а (как в CLI-chat): нативному loop
        передаётся history из trace; внешний агент (ADR-0016) продолжает
        собственную сессию по agent_session_id, history ему не нужна —
        run_once сам резолвит agent_session по session_id.
        """
        if self.quota_guard is not None:
            await self.quota_guard()  # QuotaExceededError → 429
        external = self.cfg.executor.type == "external"

        async def action(db: AsyncSession) -> tuple[Session, list[dict[str, str]]]:
            session = await find_session_by_prefix(db, session_id)
            if external:
                return session, []
            raw = await TraceRecorder(db).session_history(
                session.id, limit_messages=_SESSION_HISTORY_LIMIT
            )
            return session, raw

        session, raw = await self._read(action)
        workspace = Path((session.meta or {}).get("workspace") or self.workspace)
        if not workspace.is_dir():
            raise UnknownWorkspaceError(
                f"workspace сессии {session.id[:8]} больше не существует: {workspace}"
            )
        if await self._workspace_busy(workspace):
            raise WorkspaceBusyError(f"в сессии {session.id[:8]} ещё выполняется предыдущий run")
        history = (
            None
            if external
            else [
                ChatMessage(
                    role="user" if m["role"] == "user" else "assistant", content=m["content"]
                )
                for m in raw
            ]
        )
        mode = autonomy if autonomy is not None else self.cfg.runtime.autonomy
        # Тёплый sandbox сессии (ADR-0017): env/infra/MCP переживают сообщение.
        warm = await self._acquire_warm(session.id, workspace, mode)
        runner = warm.runner if warm is not None else self._runner_for(workspace)
        started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._spawn(
            self._run_bg(
                text,
                mode,
                started,
                runner=runner,
                session_id=session.id,
                history=history,
                warm=warm,
            )
        )
        return await started

    async def get_session(self, session_id: str) -> SessionView:
        async def action(db: AsyncSession) -> SessionView:
            session = await find_session_by_prefix(db, session_id)
            result = await db.execute(
                select(Run).where(Run.session_id == session.id).order_by(Run.created_at)
            )
            runs = [_summary(run) for run in result.scalars()]
            return SessionView(
                session_id=session.id,
                title=session.title or "",
                workspace=(session.meta or {}).get("workspace"),
                runs=runs,
            )

        return await self._read(action)

    # --- named workspaces и артефакты (ADR-0017) ---------------------------

    async def create_workspace(self, name: str) -> WorkspaceView:
        path = create_named_workspace(
            self.workspace, name, limit=self.cfg.cloud.max_named_workspaces
        )
        return WorkspaceView(name=name, size_bytes=0, modified_at=_mtime(path), busy=False)

    async def list_workspaces(self) -> list[WorkspaceView]:
        views = []
        for info in list_named_workspaces(self.workspace):
            views.append(
                WorkspaceView(
                    name=info.name,
                    size_bytes=info.size_bytes,
                    modified_at=info.modified_at,
                    busy=await self._workspace_busy(info.path.resolve()),
                )
            )
        return views

    async def delete_workspace(self, name: str) -> None:
        path = resolve_named_workspace(self.workspace, name).resolve()
        if await self._workspace_busy(path):
            raise WorkspaceBusyError(f"workspace '{name}' занят активным run — удаление отклонено")
        # Тёплые sandbox'ы сессий этого workspace закрываем до rmtree —
        # иначе контейнер останется с mount'ом удалённого дерева.
        for session_id, slot in list(self._warm.items()):
            if slot.workspace == path:
                await self._drop_warm(session_id)
        delete_named_workspace(self.workspace, name)

    def workspace_target(self, name: str, relative: str) -> Path:
        """Файл/каталог внутри named workspace (confinement — в provision)."""
        return resolve_workspace_file(self.workspace, name, relative)

    def archive_workspace(self, name: str) -> Path:
        """tar.gz снапшот named workspace во временном файле (вызывающий удаляет)."""
        base = resolve_named_workspace(self.workspace, name)
        fd, tmp = tempfile.mkstemp(prefix=f"svarog-ws-{name}-", suffix=".tar.gz")
        os.close(fd)
        with tarfile.open(tmp, "w:gz") as tar:
            # tarfile не следует symlink'ам (кладёт их как symlink-записи) —
            # содержимое за пределами workspace в архив не утекает.
            tar.add(base, arcname=name)
        return Path(tmp)

    async def run_diff(self, run_id: str) -> RunDiffView:
        """Диф run'а: патч его step-коммитов (Run-Id trailer, Flow C) +
        незакоммиченные изменения рабочего дерева (ADR-0017 §2)."""

        async def action(db: AsyncSession) -> Run:
            return await find_run_by_prefix(db, run_id)

        run = await self._read(action)
        workspace = Path(run.workspace) if run.workspace else self.workspace
        committed = uncommitted = ""
        repo = GitRepo(workspace)
        if workspace.is_dir() and await repo.is_repo() and await repo.has_commits():
            _, uncommitted, _ = await repo._git("diff", "HEAD", check=False)
            shas = [sha for sha, rid in await repo.log_with_run_ids() if run.id in rid.split(",")]
            if shas:
                newest, oldest = shas[0], shas[-1]
                code, base, _ = await repo._git("rev-parse", f"{oldest}^", check=False)
                base_ref = base.strip() if code == 0 else _EMPTY_TREE
                _, committed, _ = await repo._git("diff", base_ref, newest, check=False)
        return RunDiffView(run_id=run.id, committed=committed, uncommitted=uncommitted)

    async def sweep_workspaces(self) -> list[Path]:
        """Retention-GC терминальных task-workspace'ов (named не трогает)."""
        days = self.cfg.cloud.workspace_retention_days
        if days <= 0:
            return []

        async def action(db: AsyncSession) -> set[str]:
            result = await db.execute(
                select(Run.workspace).where(Run.state.in_(_LIVE_STATES), Run.workspace.is_not(None))
            )
            return {ws for (ws,) in result.all() if ws}

        active = await self._read(action)
        return sweep_task_workspaces(self.workspace, retention_days=days, active=active)

    async def _maybe_sweep_workspaces(self) -> None:
        if self.cfg.cloud.workspace_retention_days <= 0:
            return
        now = time.monotonic()
        if self._last_gc and now - self._last_gc < _GC_INTERVAL_SEC:
            return
        self._last_gc = now
        with contextlib.suppress(Exception):
            await self.sweep_workspaces()

    # --- супервизор refuel (§6.10, ADR-0005) ------------------------------

    async def supervise_once(self) -> list[str]:
        """Один проход: поднять refuel-suspended runs. Возвращает run_id'ы, для
        которых запущено авто-возобновление (для тестов и наблюдаемости)."""
        await self._maybe_sweep_workspaces()  # retention-GC task-workspaces (ADR-0017)
        with contextlib.suppress(Exception):
            await self._sweep_warm_sessions()  # idle-GC тёплых sandbox'ов (ADR-0017)
        sup = self.cfg.supervisor
        if not sup.auto_resume_refuel:
            return []

        async def fetch(db: AsyncSession) -> list[Run]:
            return await TraceRecorder(db).find_refuel_suspended_runs()

        resumed: list[str] = []
        for run in await self._read(fetch):
            if run.id in self._inflight:
                continue  # авто-resume уже в полёте — не дублируем
            if self._auto_resumes.get(run.id, 0) >= sup.max_auto_resumes:
                continue  # предохранитель от петли исчерпан
            self._auto_resumes[run.id] = self._auto_resumes.get(run.id, 0) + 1
            self._spawn_supervised_resume(run.id)
            resumed.append(run.id)
        return resumed

    def _spawn_supervised_resume(self, run_id: str) -> None:
        self._inflight.add(run_id)
        self.events.reset(run_id)

        async def wrapped() -> None:
            try:
                await self._resume_bg(run_id)
            finally:
                self._inflight.discard(run_id)

        self._spawn(wrapped())

    async def run_supervisor(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        """Периодически поднимать refuel-suspended runs (§6.10).

        Живёт в долгоживущих процессах (serve/telegram); останавливается по
        should_stop или отмене задачи (lifespan/сигнал). Ошибка прохода не рвёт
        цикл. Естественный потолок числа возобновлений — max_iterations run'а,
        поверх него — supervisor.max_auto_resumes.
        """
        interval = self.cfg.supervisor.interval_sec
        while should_stop is None or not should_stop():
            with contextlib.suppress(Exception):
                await self.supervise_once()
            await asyncio.sleep(interval)

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


def _mtime(path: Path) -> "datetime":
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


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
