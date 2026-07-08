"""Оркестрация одного прогона задачи (§6.2, §11) — общая для всех интерфейсов.

CLI, gateway (REST/WS) и Telegram гоняют один и тот же end-to-end цикл:
подготовка workspace (Flow C) → sandbox → agent loop → слив памяти (Flow A)
→ verifier → auto-commit. Раньше он жил в `cli/main.py` вперемешку с выводом
в консоль; вынесен сюда, а весь пользовательский вывод идёт через `RunHooks`.
Так gateway не дублирует ядро (repo-structure: `cli`/`gateway` → `runtime`).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.loader import load_config
from svarog_harness.config.paths import first_existing_skills_dir, memory_dir, skills_dirs
from svarog_harness.config.schema import AutonomyMode, SvarogConfig
from svarog_harness.gitflow import GitRepo, SecretScanBlockedError, WorkspaceFlow, WorkspacePrep
from svarog_harness.llm.openai_compatible import default_provider
from svarog_harness.memory import MemoryWriter, read_memory
from svarog_harness.policy import PolicyEngine, load_policy_rules
from svarog_harness.runtime.checkpoint import LoopState
from svarog_harness.runtime.loop import AgentLoop, RunOutcome
from svarog_harness.sandbox import ExecutionEnvironment, create_environment
from svarog_harness.secrets import SecretStore, default_secret_store, injected_env
from svarog_harness.skills import Skill, scan_skills, skill_cards
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import Run, RunState
from svarog_harness.tools.approval import RequestApprovalTool
from svarog_harness.tools.file_tools import file_tools
from svarog_harness.tools.memory_tools import RememberTool
from svarog_harness.tools.registry import ToolRegistry
from svarog_harness.tools.shell import BashTool
from svarog_harness.tools.skill_tools import ReadSkillTool
from svarog_harness.trace.lookup import RunNotResumableError
from svarog_harness.trace.recorder import TraceRecorder
from svarog_harness.verifier import CheckOutcome, Verifier, skill_checks


@dataclass
class RunHooks:
    """Точки наблюдения за прогоном; None — событие игнорируется.

    Интерфейс (CLI, gateway, Telegram) подставляет свои реализации:
    печать в консоль, публикация в event-stream, отправка в чат.
    """

    on_skill_skipped: Callable[[str, str], None] | None = None
    on_workspace_prep: Callable[[WorkspacePrep], None] | None = None
    on_recovered: Callable[[Run], None] | None = None
    on_run_started: Callable[[Run], None] | None = None
    on_text_delta: Callable[[str], None] | None = None
    on_tool_call: Callable[[str, dict[str, object]], None] | None = None
    on_notify: Callable[[str, str], None] | None = None
    on_check: Callable[[CheckOutcome], None] | None = None
    on_verify_failed: Callable[[int], None] | None = None
    on_commit: Callable[[str, str, bool], None] | None = None
    on_commit_blocked: Callable[[str], None] | None = None
    on_memory: Callable[[str | None, str | None], None] | None = None


class TaskRunner:
    """Гоняет задачи в фиксированном workspace по одной конфигурации."""

    def __init__(self, cfg: SvarogConfig, workspace: Path) -> None:
        self._cfg = cfg
        self._workspace = workspace
        self._store: SecretStore = default_secret_store(cfg.secrets.path)

    @property
    def store(self) -> SecretStore:
        return self._store

    async def with_db[T](self, action: Callable[[AsyncSession], Awaitable[T]]) -> T:
        init_db(self._cfg.storage.db_path)
        engine = create_engine(self._cfg.storage.db_path.expanduser())
        try:
            factory = create_session_factory(engine)
            async with factory() as db:
                return await action(db)
        finally:
            await engine.dispose()

    def build_environment(self) -> ExecutionEnvironment:
        return create_environment(
            self._cfg.sandbox,
            self._workspace,
            skills_dir=first_existing_skills_dir(self._cfg, self._workspace),
            env=injected_env(self._store, self._cfg.secrets.inject),  # только явно выданные (§12)
        )

    async def recover(self, recorder: TraceRecorder, hooks: RunHooks) -> None:
        """Recovery незавершённых runs при старте (ADR-0005)."""
        for run in await recorder.recover_interrupted_runs():
            if hooks.on_recovered is not None:
                hooks.on_recovered(run)

    def build_loop(
        self,
        recorder: TraceRecorder,
        environment: ExecutionEnvironment,
        autonomy: AutonomyMode,
        hooks: RunHooks,
    ) -> AgentLoop:
        # Режим автономии и policy-правила фиксируются здесь, при старте run,
        # и не перечитываются во время исполнения (ADR-0010).
        cfg, workspace, store = self._cfg, self._workspace, self._store
        policy = PolicyEngine(
            autonomy=autonomy,
            policies=cfg.policies,
            workspace=workspace,
            rules=load_policy_rules(workspace),
            skills_dirs=skills_dirs(cfg, workspace),
        )
        scan = scan_skills(skills_dirs(cfg, workspace))
        for skill_error in scan.errors:
            if hooks.on_skill_skipped is not None:
                hooks.on_skill_skipped(skill_error.path.name, skill_error.reason)
        mem_dir = memory_dir(cfg)
        memory_text = (
            read_memory(mem_dir, limit_bytes=cfg.memory.context_limit_bytes)
            if mem_dir is not None
            else ""
        )
        skill_load_sink: list[tuple[str, str | None]] = []
        memory_sink: list[dict[str, object]] = []
        registry = self._build_registry(
            environment,
            scan.skills,
            skill_load_sink,
            memory_sink,
            memory_enabled=mem_dir is not None,
        )
        return AgentLoop(
            default_provider(cfg.models, store),
            registry,
            recorder,
            cfg.runtime,
            policy,
            workspace,
            model_name=cfg.models.providers[cfg.models.default].model,
            skill_cards=skill_cards(scan.skills),
            memory=memory_text,
            skill_load_sink=skill_load_sink,
            memory_sink=memory_sink,
            workspace_flow=WorkspaceFlow(GitRepo(workspace), cfg.git),
            secret_values=store.values(),
            on_text_delta=hooks.on_text_delta,
            on_tool_call=hooks.on_tool_call,
            on_notify=hooks.on_notify,
            on_run_started=hooks.on_run_started,
        )

    def _build_registry(
        self,
        environment: ExecutionEnvironment,
        skills: list[Skill],
        skill_load_sink: list[tuple[str, str | None]],
        memory_sink: list[dict[str, object]],
        *,
        memory_enabled: bool,
    ) -> ToolRegistry:
        registry = ToolRegistry()
        for tool in file_tools(self._workspace):
            registry.register(tool)
        registry.register(BashTool(environment, self._cfg.sandbox.timeout_sec))
        registry.register(RequestApprovalTool())
        if skills:
            registry.register(
                ReadSkillTool(
                    skills, on_load=lambda name, version: skill_load_sink.append((name, version))
                )
            )
        if memory_enabled:
            registry.register(
                RememberTool(on_enqueue=lambda req: memory_sink.append(req.to_dict()))
            )
        return registry

    async def run_once(self, task: str, autonomy: AutonomyMode, *, hooks: RunHooks) -> RunOutcome:
        """Полный прогон: workspace prep → sandbox → loop → память → verifier → commit."""
        flow = WorkspaceFlow(GitRepo(self._workspace), self._cfg.git)
        prep = await flow.start(task)
        if hooks.on_workspace_prep is not None:
            hooks.on_workspace_prep(prep)

        environment = self.build_environment()
        await environment.start()
        try:

            async def action(db: AsyncSession) -> RunOutcome:
                recorder = TraceRecorder(db)
                await self.recover(recorder, hooks)
                loop = self.build_loop(recorder, environment, autonomy, hooks)
                outcome = await loop.run(task, autonomy)
                await self.drain_memory(db, hooks)
                await self.verify(environment, recorder, outcome, hooks)
                await self._autocommit(flow, prep, task, outcome, hooks)
                return outcome

            return await self.with_db(action)
        finally:
            await environment.cleanup()

    async def resume(self, run_id: str, *, hooks: RunHooks) -> RunOutcome:
        """Возобновить run из checkpoint (ADR-0005).

        БД берётся из entry-конфигурации этого runner'а, а рабочая директория
        и её runtime/sandbox-настройки — из конфига workspace'а checkpoint'а;
        режим автономии заморожен в самом run (ADR-0010).
        """

        async def action(db: AsyncSession) -> RunOutcome:
            recorder = TraceRecorder(db)
            await self.recover(recorder, hooks)
            run, raw_state = await recorder.load_resumable(run_id)
            state = LoopState.from_dict(raw_state)
            workspace = state.workspace
            if not workspace.is_dir():
                raise RunNotResumableError(f"workspace run'а больше не существует: {workspace}")
            runner = TaskRunner(load_config(project_dir=workspace), workspace)
            environment = runner.build_environment()
            await environment.start()
            try:
                loop = runner.build_loop(recorder, environment, AutonomyMode(run.autonomy), hooks)
                outcome = await loop.resume(run, state)
                await runner.drain_memory(db, hooks)
                await runner.verify(environment, recorder, outcome, hooks)
                return outcome
            finally:
                await environment.cleanup()

        return await self.with_db(action)

    async def verify(
        self,
        environment: ExecutionEnvironment,
        recorder: TraceRecorder,
        outcome: RunOutcome,
        hooks: RunHooks,
    ) -> None:
        """Детерминированный verifier после completed-run (§6.11); пишет CheckResult."""
        if outcome.state is not RunState.COMPLETED:
            return
        run = await recorder.get_run(outcome.run_id)
        if run is None:
            return
        cfg = self._cfg
        scan = scan_skills(skills_dirs(cfg, self._workspace))
        loaded = await recorder.loaded_skill_names(run)
        checks = [*cfg.verifier.checks, *skill_checks(scan.skills, loaded)]
        if not checks and not cfg.verifier.secret_scan:
            return
        verifier = Verifier(environment, self._workspace)
        outcomes = await verifier.run(
            checks, secret_scan=cfg.verifier.secret_scan, known_values=self._store.values()
        )
        failed = [o for o in outcomes if not o.passed]
        for check in outcomes:
            await recorder.log_check_result(
                run, name=check.name, status=check.status, output=check.output
            )
            if hooks.on_check is not None:
                hooks.on_check(check)
        if failed and hooks.on_verify_failed is not None:
            hooks.on_verify_failed(len(failed))

    async def drain_memory(self, db: AsyncSession, hooks: RunHooks) -> None:
        """Применить очередь заявок памяти single writer'ом после run (ADR-0004)."""
        mem_dir = memory_dir(self._cfg)
        if mem_dir is None or not mem_dir.is_dir():
            return
        writer = MemoryWriter(db, mem_dir)
        for row in await writer.drain():
            if hooks.on_memory is not None:
                hooks.on_memory(row.commit_sha, row.error)

    async def _autocommit(
        self,
        flow: WorkspaceFlow,
        prep: WorkspacePrep,
        task: str,
        outcome: RunOutcome,
        hooks: RunHooks,
    ) -> None:
        """Flow C: закоммитить изменения workspace на task-ветке после run."""
        cfg = self._cfg
        if not (prep.is_git and cfg.git.auto_commit and outcome.state is RunState.COMPLETED):
            return
        try:
            sha = await flow.commit_step(f"svarog: {task[:72]}", run_id=outcome.run_id)
        except SecretScanBlockedError as exc:
            if hooks.on_commit_blocked is not None:
                hooks.on_commit_blocked(str(exc))
            return
        if sha is None:
            return
        if hooks.on_commit is not None:
            hooks.on_commit(sha, prep.branch or "HEAD", cfg.git.require_approval_for_push)
