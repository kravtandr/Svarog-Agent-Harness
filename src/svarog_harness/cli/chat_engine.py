"""Движок chat-сессии, общий для plain-REPL и TUI (ADR-0018).

Каждое сообщение — отдельный run в общей session (§10.1). Движок владеет
тёплым sandbox'ом серии (`SessionResources`) и одной DB-сессией на весь
диалог; фронтенды (plain-REPL в `cli.main`, Textual TUI в `cli.tui`)
наблюдают прогон через `RunHooks` и не знают о native/external-ветвлении.
Движок print-free: всё человекочитаемое уходит фронтенду через hooks и
`ChatSessionStart.label`.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from svarog_harness.config.paths import assert_workspace_isolated
from svarog_harness.config.schema import AutonomyMode, SvarogConfig
from svarog_harness.llm.provider import ChatMessage
from svarog_harness.mcp import MCPTool, build_mcp_tools
from svarog_harness.runtime.loop import RunOutcome
from svarog_harness.runtime.orchestrator import RunHooks, SessionResources, TaskRunner
from svarog_harness.skills.curator import CuratorStore
from svarog_harness.skills.proposal import SkillProposalRequest
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import Approval
from svarog_harness.trace.lookup import find_session_by_prefix
from svarog_harness.trace.recorder import TraceRecorder
from svarog_harness.trace.viewer import SessionSummary, fetch_sessions

CHAT_HISTORY_LIMIT = 24  # сообщений диалога в контексте, чтобы не раздувать промпт


async def with_db[T](cfg: SvarogConfig, action: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Короткая DB-сессия под одно действие (паттерн `TaskRunner.with_db`)."""
    init_db(cfg.storage.db_path)
    engine = create_engine(cfg.storage.db_path.expanduser())
    try:
        factory = create_session_factory(engine)
        async with factory() as db:
            return await action(db)
    finally:
        await engine.dispose()


def record_gate_decision(
    cfg: SvarogConfig, approval_id: str, *, approved: bool, reason: str | None, decided_by: str
) -> None:
    """Записать решение approval собственной короткой DB-сессией.

    Вызывается из worker-потока живого гейта (§7): у потока нет running loop,
    поэтому собственный `asyncio.run` безопасен; poll гейта подхватит решение.
    """

    async def decide(db: AsyncSession) -> None:
        recorder = TraceRecorder(db)
        found = await recorder.find_approval_by_prefix(approval_id)
        await recorder.decide_approval(
            found, approved=approved, decided_by=decided_by, reason=reason
        )

    asyncio.run(with_db(cfg, decide))


def record_gate_answer(
    cfg: SvarogConfig, approval_id: str, answer: str, *, answered_by: str
) -> None:
    """Записать ответ на ask_user (§6.5) собственной короткой DB-сессией."""

    async def record(db: AsyncSession) -> None:
        recorder = TraceRecorder(db)
        found = await recorder.find_approval_by_prefix(approval_id)
        await recorder.answer_question(found, answer=answer, answered_by=answered_by)

    asyncio.run(with_db(cfg, record))


@dataclass(frozen=True)
class ChatSessionStart:
    """Итог инициализации сессии: что продолжаем и сколько истории подхвачено."""

    session_id: str | None
    history: list[ChatMessage]
    label: str | None  # "продолжаю сессию …" / "форк …" — печатает фронтенд


class ChatEngineProtocol(Protocol):
    """Контракт движка для фронтендов (TUI подменяет фейком в тестах)."""

    @property
    def session_id(self) -> str | None: ...
    @property
    def is_external(self) -> bool: ...
    async def start(
        self, *, continue_ref: str | None = None, fork_ref: str | None = None
    ) -> ChatSessionStart: ...
    async def close(self) -> None: ...
    async def send(self, task: str) -> RunOutcome: ...
    async def resume(self, run_id: str) -> RunOutcome: ...
    async def rebuild_resources(self) -> None: ...
    async def reconfigure(self, cfg: SvarogConfig, autonomy: AutonomyMode) -> None: ...
    async def pending_approvals(self, run_id: str) -> list[Approval]: ...
    async def decide_approval(
        self, approval_id: str, *, approved: bool, reason: str | None, decided_by: str
    ) -> None: ...
    async def answer_question(self, approval_id: str, answer: str, *, answered_by: str) -> None: ...
    async def list_sessions(
        self, *, limit: int = 20, search: str | None = None
    ) -> list[SessionSummary]: ...
    async def session_preview(self, session_id: str, *, limit: int = 6) -> list[dict[str, str]]: ...
    async def switch_session(self, ref: str, *, fork: bool) -> ChatSessionStart: ...
    def reset_session(self) -> None: ...


class ChatEngine:
    """Драйвер chat-сессии: тёплый sandbox, каждое сообщение — run (§10.1).

    Перенос тела `_chat_session` (cli.main) без изменения поведения: та же
    пара native/external-веток, drain памяти и skill-proposals после каждого
    сообщения, лимит истории CHAT_HISTORY_LIMIT. Lifecycle env/infra/MCP —
    через `TaskRunner.prepare_session_resources` (ADR-0017).
    """

    def __init__(
        self,
        cfg: SvarogConfig,
        workspace: Path,
        autonomy: AutonomyMode,
        hooks: RunHooks,
        *,
        allow_layout_overlap: bool = False,
    ) -> None:
        self._cfg = cfg
        self._workspace = workspace
        self._autonomy = autonomy
        self._hooks = hooks
        # Подтверждённое человеком пересечение с control-plane (ADR-0018).
        self._allow_layout_overlap = allow_layout_overlap
        self._external = cfg.executor.type == "external"
        self._runner: TaskRunner | None = None
        self._resources: SessionResources | None = None
        self._mcp_tools: list[MCPTool] = []
        self._db_engine: AsyncEngine | None = None
        self._db: AsyncSession | None = None
        self._recorder: TraceRecorder | None = None
        self._session_id: str | None = None
        self._history: list[ChatMessage] = []
        self._resources_dirty = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def is_external(self) -> bool:
        return self._external

    async def start(
        self, *, continue_ref: str | None = None, fork_ref: str | None = None
    ) -> ChatSessionStart:
        """Поднять sandbox и DB-сессию; подхватить историю continue/fork."""
        # Раскладка workspace (ADR-0015 §0.3) — как в run_once/resume.
        assert_workspace_isolated(
            self._cfg, self._workspace, allow_overlap=self._allow_layout_overlap
        )
        self._runner = TaskRunner(
            self._cfg, self._workspace, allow_layout_overlap=self._allow_layout_overlap
        )
        # prepare_session_resources сам делает fail-closed проверки sandbox и
        # автономии внешнего агента (ADR-0013/ADR-0016 §6).
        self._resources = await self._runner.prepare_session_resources(self._autonomy)
        # MCP внешнему агенту не пробрасывается (у него bridge, ADR-0016 §4):
        # backends в этом случае пустые ещё с prepare_session_resources.
        self._mcp_tools = build_mcp_tools(self._resources.backends)
        init_db(self._cfg.storage.db_path)
        self._db_engine = create_engine(self._cfg.storage.db_path.expanduser())
        self._db = create_session_factory(self._db_engine)()
        self._recorder = TraceRecorder(self._db)
        await self._runner.recover(self._recorder, self._hooks)
        label: str | None = None
        if continue_ref or fork_ref:
            # Продолжение/форк сессии (ADR-0015 фаза 5): история из trace —
            # пары (task, финальный ответ) по каждому run сессии.
            start = await self._load_session(continue_ref or fork_ref or "", fork=not continue_ref)
            label = start.label
        return ChatSessionStart(
            session_id=self._session_id, history=list(self._history), label=label
        )

    async def close(self) -> None:
        """Идемпотентно опустить sandbox и DB; ошибки шагов не блокируют друг друга."""
        if self._resources is not None:
            await self._resources.close()
            self._resources = None
        if self._db is not None:
            await self._db.close()
            self._db = None
            self._recorder = None
        if self._db_engine is not None:
            await self._db_engine.dispose()
            self._db_engine = None

    async def __aenter__(self) -> "ChatEngine":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def _require_started(self) -> tuple[TaskRunner, SessionResources, AsyncSession, TraceRecorder]:
        assert (
            self._runner is not None
            and self._resources is not None
            and self._db is not None
            and self._recorder is not None
        ), "ChatEngine.start() не вызван"
        return self._runner, self._resources, self._db, self._recorder

    async def send(self, task: str) -> RunOutcome:
        """Одно сообщение диалога — один run; после него drain память/proposals."""
        runner, resources, db, recorder = self._require_started()
        if self._resources_dirty:
            # Прерванный run (Esc в TUI) мог оставить sandbox в полумёртвом
            # состоянии — пересобираем, как gateway при сбое тёплой сессии.
            await self.rebuild_resources()
            runner, resources, db, recorder = self._require_started()
        proposal_sink: list[SkillProposalRequest] = []
        try:
            if self._external:
                # Chat поверх agent-сессий (ADR-0016 фаза 3): контекст диалога
                # живёт в сессии агента — продолжаем её --resume.
                assert resources.infra is not None
                control = runner.wire_bridge_control(
                    resources.infra, self._autonomy, proposal_sink, self._hooks
                )
                agent_session = (
                    await recorder.last_agent_session(self._session_id)
                    if self._session_id is not None
                    else None
                )
                executor = runner.build_external_executor(
                    recorder,
                    resources.environment,
                    self._hooks,
                    infra=resources.infra,
                    control=control,
                )
                outcome = await executor.run(
                    task, self._autonomy, session_id=self._session_id, agent_session=agent_session
                )
            else:
                excluded = frozenset(await CuratorStore(db).archived_names())
                loop = runner.build_loop(
                    recorder,
                    resources.environment,
                    self._autonomy,
                    self._hooks,
                    proposal_sink,
                    excluded_skills=excluded,
                    mcp_tools=self._mcp_tools,
                )
                outcome = await loop.run(
                    task, self._autonomy, session_id=self._session_id, history=list(self._history)
                )
        except asyncio.CancelledError:
            self._resources_dirty = True
            raise
        await runner.drain_memory(db, self._hooks)
        await runner.drain_proposals(db, proposal_sink, outcome.run_id, self._hooks)
        if self._session_id is None:
            run = await recorder.get_run(outcome.run_id)
            self._session_id = run.session_id if run else None
        self._history.append(ChatMessage(role="user", content=task))
        self._history.append(
            ChatMessage(role="assistant", content=outcome.final_answer or "(без ответа)")
        )
        self._history[:] = self._history[-CHAT_HISTORY_LIMIT:]
        return outcome

    async def resume(self, run_id: str) -> RunOutcome:
        """Возобновить run сессии (WAITING_APPROVAL → решение → продолжение)."""
        runner, _, _, _ = self._require_started()
        outcome = await runner.resume(run_id, hooks=self._hooks)
        # Финальный ответ после resume замещает «ожидающий» ответ в истории.
        if self._history and self._history[-1].role == "assistant":
            self._history[-1] = ChatMessage(
                role="assistant", content=outcome.final_answer or "(без ответа)"
            )
        return outcome

    async def rebuild_resources(self) -> None:
        """Пересобрать тёплый sandbox (после отмены run'а)."""
        runner = self._runner
        assert runner is not None, "ChatEngine.start() не вызван"
        if self._resources is not None:
            await self._resources.close()
            self._resources = None
        self._resources = await runner.prepare_session_resources(self._autonomy)
        self._mcp_tools = build_mcp_tools(self._resources.backends)
        self._resources_dirty = False

    async def reconfigure(self, cfg: SvarogConfig, autonomy: AutonomyMode) -> None:
        """Сменить cfg/autonomy mid-session и пересобрать sandbox (/executor и др.)."""
        self._cfg = cfg
        self._autonomy = autonomy
        self._external = cfg.executor.type == "external"
        if self._runner is None:
            return
        self._runner = TaskRunner(
            cfg, self._workspace, allow_layout_overlap=self._allow_layout_overlap
        )
        await self.rebuild_resources()

    async def pending_approvals(self, run_id: str) -> list[Approval]:
        _, _, _, recorder = self._require_started()
        pending = await recorder.fetch_pending_approvals()
        return [a for a in pending if a.run_id == run_id]

    async def decide_approval(
        self, approval_id: str, *, approved: bool, reason: str | None, decided_by: str
    ) -> None:
        _, _, _, recorder = self._require_started()
        found = await recorder.find_approval_by_prefix(approval_id)
        await recorder.decide_approval(
            found, approved=approved, decided_by=decided_by, reason=reason
        )

    async def answer_question(self, approval_id: str, answer: str, *, answered_by: str) -> None:
        _, _, _, recorder = self._require_started()
        found = await recorder.find_approval_by_prefix(approval_id)
        await recorder.answer_question(found, answer=answer, answered_by=answered_by)

    async def list_sessions(
        self, *, limit: int = 20, search: str | None = None
    ) -> list[SessionSummary]:
        _, _, db, _ = self._require_started()
        return await fetch_sessions(db, limit=limit, search=search)

    async def session_preview(self, session_id: str, *, limit: int = 6) -> list[dict[str, str]]:
        _, _, _, recorder = self._require_started()
        return await recorder.session_history(session_id, limit_messages=limit)

    async def switch_session(self, ref: str, *, fork: bool) -> ChatSessionStart:
        """Переключиться на другую сессию между runs (продолжить или форкнуть)."""
        start = await self._load_session(ref, fork=fork)
        return start

    def reset_session(self) -> None:
        """/new: следующий send начнёт новую сессию с чистой историей."""
        self._session_id = None
        self._history = []

    async def _load_session(self, ref: str, *, fork: bool) -> ChatSessionStart:
        _, _, db, recorder = self._require_started()
        source = await find_session_by_prefix(db, ref)
        raw = await recorder.session_history(source.id, limit_messages=CHAT_HISTORY_LIMIT)
        self._history = [
            ChatMessage(role="user" if m["role"] == "user" else "assistant", content=m["content"])
            for m in raw
        ]
        if fork:
            self._session_id = None
            label = f"форк сессии {source.id[:8]} — новая сессия"
        else:
            self._session_id = source.id
            label = f"продолжаю сессию {source.id[:8]}"
        return ChatSessionStart(
            session_id=self._session_id,
            history=list(self._history),
            label=f"{label} ({len(self._history)} сообщений истории)",
        )
