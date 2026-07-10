"""Оркестрация одного прогона задачи (§6.2, §11) — общая для всех интерфейсов.

CLI, gateway (REST/WS) и Telegram гоняют один и тот же end-to-end цикл:
подготовка workspace (Flow C) → sandbox → agent loop → слив памяти (Flow A)
→ verifier → auto-commit. Раньше он жил в `cli/main.py` вперемешку с выводом
в консоль; вынесен сюда, а весь пользовательский вывод идёт через `RunHooks`.
Так gateway не дублирует ядро (repo-structure: `cli`/`gateway` → `runtime`).
"""

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.loader import load_config
from svarog_harness.config.paths import (
    assert_workspace_isolated,
    clamp_by_role,
    first_existing_skills_dir,
    memory_dir,
    skills_dirs,
    workspace_layout_violations,
)
from svarog_harness.config.schema import AutonomyMode, SvarogConfig, TenantRole
from svarog_harness.gitflow import GitRepo, SecretScanBlockedError, WorkspaceFlow, WorkspacePrep
from svarog_harness.llm.openai_compatible import default_provider
from svarog_harness.mcp import MCPBackend, MCPTool, build_mcp_tools, connect_mcp_servers
from svarog_harness.memory import MemoryWriter, read_memory
from svarog_harness.policy import PolicyEngine, load_policy_rules
from svarog_harness.runtime.checkpoint import LoopState
from svarog_harness.runtime.config_snapshot import CONFIG_HASH_META_KEY, config_digest
from svarog_harness.runtime.loop import AgentLoop, RunOutcome
from svarog_harness.sandbox import (
    ExecutionEnvironment,
    SandboxError,
    create_environment,
    find_docker,
)
from svarog_harness.secrets import SecretStore, default_secret_store, injected_env, selected_values
from svarog_harness.skills import Skill, scan_skills, skill_cards
from svarog_harness.skills.curator import CuratorStore
from svarog_harness.skills.proposal import SkillProposalRequest
from svarog_harness.skills.proposal_manager import SkillProposalManager
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.locks import LockBackend, default_lock_backend
from svarog_harness.storage.models import Run, RunState, SkillProposal
from svarog_harness.tools.approval import RequestApprovalTool
from svarog_harness.tools.file_tools import file_tools
from svarog_harness.tools.memory_tools import ReadMemoryTool, RememberTool
from svarog_harness.tools.plan_tools import UpdatePlanTool
from svarog_harness.tools.registry import ToolRegistry
from svarog_harness.tools.shell import BashTool
from svarog_harness.tools.skill_tools import CreateSkillProposalTool, ReadSkillTool
from svarog_harness.tools.user_tools import AskUserTool
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
    on_proposal: Callable[[SkillProposal], None] | None = None


class ConfigDriftError(Exception):
    """Security-конфиг run'а изменился между стартом и resume (ADR-0015 §0.4)."""


def _assert_config_unchanged(run: Run, cfg: SvarogConfig, workspace: Path) -> None:
    """Fail-closed при расхождении текущего конфига со снимком старта run'а.

    Снимок отсутствует (run стартовал до §0.4) — пропускаем: нечего сверять,
    но и не выдумываем расхождение. Иначе сверяем хеши: несовпадение отклоняет
    resume, чтобы run не продолжился под подменённым провайдером/MCP/policy.
    """
    stored = (run.meta or {}).get(CONFIG_HASH_META_KEY)
    if stored is None:
        return
    current = config_digest(cfg, workspace)
    if current != stored:
        raise ConfigDriftError(
            f"security-конфиг run {run.id[:8]} изменился с момента старта "
            f"(провайдер/MCP/policy/secrets-refs): resume отклонён (fail-closed, §0.4). "
            f"Верните исходный конфиг workspace'а или запустите новый run"
        )


async def _close_backends(backends: list[MCPBackend]) -> None:
    for backend in backends:
        with contextlib.suppress(Exception):
            await backend.close()


class TaskRunner:
    """Гоняет задачи в фиксированном workspace по одной конфигурации."""

    def __init__(
        self, cfg: SvarogConfig, workspace: Path, *, role: TenantRole = TenantRole.SUPERUSER
    ) -> None:
        # Роль фиксируется при старте (ADR-0010/0013) и заклампывает cfg
        # идемпотентно: standard → docker/hardened, superuser (по умолчанию) —
        # no-op. Клампим здесь, чтобы гарантия держалась и на resume (см. resume).
        self._role = role
        self._cfg = clamp_by_role(cfg, role)
        cfg = self._cfg
        self._workspace = workspace
        # Два скоупа секретов (ADR-0014 #2):
        #   _store — SANDBOX-инъекция (secrets.inject): у standard env_fallback
        #     выключен клампом, чтобы tenant-ref не провалился в хостовый os.environ;
        #   _host_store — HOST-side резолвинг (provider api_key_ref, MCP env_refs,
        #     gateway): всегда с env-fallback. Значения используются в host-процессе
        #     (LLM-вызов, spawn MCP), в sandbox не попадают — env здесь безопасен.
        self._store: SecretStore = default_secret_store(
            cfg.secrets.path, env_fallback=cfg.secrets.env_fallback
        )
        self._host_store: SecretStore = default_secret_store(cfg.secrets.path, env_fallback=True)
        # Межпроцессная сериализация memory-writer (ADR-0004/0007).
        self._lock: LockBackend = default_lock_backend(cfg.storage.db_path)

    @property
    def store(self) -> SecretStore:
        """Sandbox-скоуп секретов (для инъекции в контейнер)."""
        return self._store

    @property
    def host_store(self) -> SecretStore:
        """Host-скоуп секретов (provider/MCP/gateway; резолвится вне sandbox)."""
        return self._host_store

    async def with_db[T](self, action: Callable[[AsyncSession], Awaitable[T]]) -> T:
        init_db(self._cfg.storage.db_path)
        engine = create_engine(self._cfg.storage.db_path.expanduser())
        try:
            factory = create_session_factory(engine)
            async with factory() as db:
                return await action(db)
        finally:
            await engine.dispose()

    def assert_sandbox_available(self) -> None:
        """Fail-closed (ADR-0013/0014): docker-режим без доступного runtime — отказ.

        Для standard-тенанта sandbox.type заклампан в docker (ADR-0013), поэтому
        отсутствие docker/podman не откатывается на хостовое исполнение, а
        останавливает run явно и рано — до workspace prep и подключения MCP.
        """
        if self._cfg.sandbox.type == "docker" and find_docker() is None:
            raise SandboxError(
                "docker/podman недоступен, а sandbox.type=docker (fail-closed): "
                "запуск отклонён без отката на local-trusted (ADR-0013)"
            )

    def _warn_layout_tradeoff(self, hooks: RunHooks) -> None:
        """Local-trusted: пересечение workspace с control-plane не блокирует run
        (документированный trade-off §0.3/§17), но громко предупреждаем."""
        if self._cfg.sandbox.type != "local-trusted" or hooks.on_notify is None:
            return
        violations = workspace_layout_violations(self._cfg, self._workspace)
        if violations:
            hooks.on_notify(
                "workspace.layout",
                "control-plane внутри workspace (local-trusted trade-off, §0.3): "
                + "; ".join(violations),
            )

    def build_environment(self) -> ExecutionEnvironment:
        return create_environment(
            self._cfg.sandbox,
            self._workspace,
            skills_dir=first_existing_skills_dir(self._cfg, self._workspace),
            env=injected_env(self._store, self._cfg.secrets.inject),  # только явно выданные (§12)
        )

    def known_secret_values(self) -> frozenset[str]:
        """Все значения секретов, которые этот runner явно умеет разрешить.

        FileSecretStore перечислим, а env fallback нет. Поэтому добавляем значения
        всех refs, явно названных в конфигурации, чтобы redaction и secret scan
        покрывали env-backed секреты.
        """
        refs = list(self._cfg.secrets.inject)
        refs.extend(
            provider.api_key_ref
            for provider in self._cfg.models.providers.values()
            if provider.api_key_ref is not None
        )
        for server in self._cfg.mcp.servers.values():
            refs.extend(server.env_refs)
        if self._cfg.gateway.token_ref is not None:
            refs.append(self._cfg.gateway.token_ref)
        if self._cfg.telegram.token_ref is not None:
            refs.append(self._cfg.telegram.token_ref)
        # Redaction покрывает оба скоупа: host-store перечисляет тот же файл, а
        # selected_values добавляет env-backed refs (provider-ключ и пр.).
        return self._host_store.values() | selected_values(self._host_store, refs)

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
        proposal_sink: list[SkillProposalRequest] | None = None,
        *,
        excluded_skills: frozenset[str] = frozenset(),
        mcp_tools: list[MCPTool] | None = None,
    ) -> AgentLoop:
        # Режим автономии и policy-правила фиксируются здесь, при старте run,
        # и не перечитываются во время исполнения (ADR-0010).
        cfg, workspace = self._cfg, self._workspace
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
        # archived-скиллы (Curator слой 1, §18.1) не попадают в карточки и read_skill.
        active_skills = [s for s in scan.skills if s.name not in excluded_skills]
        mem_dir = memory_dir(cfg)
        # Placeholder при пустых файлах: guidance по структуре памяти в системном
        # промпте должен присутствовать всегда, когда память включена.
        memory_text = (
            read_memory(mem_dir, limit_bytes=cfg.memory.context_limit_bytes)
            or "(память пока пуста)"
            if mem_dir is not None
            else ""
        )
        skill_load_sink: list[tuple[str, str | None]] = []
        memory_sink: list[dict[str, object]] = []
        plan_update_sink: list[dict[str, object]] = []
        registry = self._build_registry(
            environment,
            active_skills,
            skill_load_sink,
            memory_sink,
            plan_update_sink,
            proposal_sink,
            mem_dir=mem_dir,
            mcp_tools=mcp_tools,
        )
        return AgentLoop(
            default_provider(cfg.models, self._host_store),  # host-скоуп (ADR-0014 #2)
            registry,
            recorder,
            cfg.runtime,
            policy,
            workspace,
            model_name=cfg.models.providers[cfg.models.default].model,
            config_hash=config_digest(cfg, workspace),  # снимок security-конфига (§0.4)
            skill_cards=skill_cards(active_skills),
            memory=memory_text,
            skill_load_sink=skill_load_sink,
            memory_sink=memory_sink,
            plan_update_sink=plan_update_sink,
            workspace_flow=WorkspaceFlow(GitRepo(workspace), cfg.git),
            secret_values=self.known_secret_values(),
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
        plan_update_sink: list[dict[str, object]],
        proposal_sink: list[SkillProposalRequest] | None,
        *,
        mem_dir: Path | None,
        mcp_tools: list[MCPTool] | None = None,
    ) -> ToolRegistry:
        registry = ToolRegistry()
        for tool in file_tools(self._workspace):
            registry.register(tool)
        registry.register(
            UpdatePlanTool(
                on_update=lambda items, note: plan_update_sink.append(
                    {"items": items, "note": note}
                )
            )
        )
        registry.register(BashTool(environment, self._cfg.sandbox.timeout_sec))
        registry.register(RequestApprovalTool())
        registry.register(AskUserTool())
        for mcp_tool in mcp_tools or []:
            # MCP tools проходят через Policy Engine как обычные (§9): по умолчанию
            # require_approval (action_type mcp.*), риск из конфига сервера.
            registry.register(mcp_tool)
        if skills:
            registry.register(
                ReadSkillTool(
                    skills, on_load=lambda name, version: skill_load_sink.append((name, version))
                )
            )
        if mem_dir is not None:
            # memory_dir передаётся для валидации заявки в момент вызова: само
            # применение происходит после run, когда модель уже отчиталась.
            registry.register(
                RememberTool(
                    on_enqueue=lambda req: memory_sink.append(req.to_dict()),
                    memory_dir=mem_dir,
                )
            )
            # Прогрессивная загрузка (ADR-0011): страницы памяти по требованию.
            registry.register(ReadMemoryTool(mem_dir))
        if proposal_sink is not None:
            # Skill governance (Flow B, §18): агент предлагает скиллы через proposal,
            # прямые правки skills/ запрещены policy.
            registry.register(CreateSkillProposalTool(on_propose=proposal_sink.append))
        return registry

    async def run_once(self, task: str, autonomy: AutonomyMode, *, hooks: RunHooks) -> RunOutcome:
        """Полный прогон: workspace prep → sandbox → loop → память → verifier → commit."""
        self.assert_sandbox_available()  # fail-closed до любой работы (ADR-0013)
        assert_workspace_isolated(self._cfg, self._workspace)  # раскладка (ADR-0015 §0.3)
        self._warn_layout_tradeoff(hooks)
        flow = WorkspaceFlow(GitRepo(self._workspace), self._cfg.git)
        prep = await flow.start(task)
        if hooks.on_workspace_prep is not None:
            hooks.on_workspace_prep(prep)

        backends = await connect_mcp_servers(self._cfg.mcp, self._host_store)  # host-скоуп
        environment = self.build_environment()
        await environment.start()
        try:
            mcp_tools = build_mcp_tools(backends)

            async def action(db: AsyncSession) -> RunOutcome:
                recorder = TraceRecorder(db)
                await self.recover(recorder, hooks)
                # Per-workspace lease (ADR-0015 §0.5): второй параллельный run на
                # том же рабочем дереве отклоняется, пока первый жив (heartbeat).
                await recorder.acquire_workspace_lease(str(self._workspace))
                proposal_sink: list[SkillProposalRequest] = []
                excluded = frozenset(await CuratorStore(db).archived_names())
                loop = self.build_loop(
                    recorder,
                    environment,
                    autonomy,
                    hooks,
                    proposal_sink,
                    excluded_skills=excluded,
                    mcp_tools=mcp_tools,
                )
                outcome = await loop.run(task, autonomy)
                await self.drain_memory(db, hooks)
                await self.drain_proposals(db, proposal_sink, outcome.run_id, hooks)
                failed_checks = await self.verify(environment, recorder, outcome, hooks)
                if failed_checks:
                    error = f"verifier: {failed_checks} проверок не прошли"
                    run = await recorder.get_run(outcome.run_id)
                    if run is not None:
                        await recorder.finish_run(run, RunState.FAILED, error=error)
                    return replace(outcome, state=RunState.FAILED, error=error)
                await self._autocommit(flow, prep, task, outcome, hooks)
                return outcome

            return await self.with_db(action)
        finally:
            await environment.cleanup()
            await _close_backends(backends)

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
            # Роль тенанта переклампывает конфиг workspace'а на resume (ADR-0013):
            # standard остаётся в docker/hardened, даже если yaml workspace'а
            # говорит local-trusted. Для superuser (CLI-resume) — no-op.
            runner = TaskRunner(load_config(project_dir=workspace), workspace, role=self._role)
            runner.assert_sandbox_available()  # fail-closed на resume (ADR-0013)
            assert_workspace_isolated(runner._cfg, workspace)  # раскладка (ADR-0015 §0.3)
            # Trust gate (ADR-0015 §0.4): security-конфиг заморожен снимком на
            # старте. Расхождение с текущим yaml → fail-closed: resume отклонён,
            # а не тихо исполнен под подменённым провайдером/MCP/policy.
            _assert_config_unchanged(run, runner._cfg, workspace)
            # Per-workspace lease (ADR-0015 §0.5): не поднимать resume, если на том
            # же workspace уже крутится другой живой run.
            await recorder.acquire_workspace_lease(str(workspace))
            backends = await connect_mcp_servers(runner._cfg.mcp, runner._host_store)
            environment = runner.build_environment()
            await environment.start()
            try:
                proposal_sink: list[SkillProposalRequest] = []
                excluded = frozenset(await CuratorStore(db).archived_names())
                loop = runner.build_loop(
                    recorder,
                    environment,
                    AutonomyMode(run.autonomy),
                    hooks,
                    proposal_sink,
                    excluded_skills=excluded,
                    mcp_tools=build_mcp_tools(backends),
                )
                outcome = await loop.resume(run, state)
                await runner.drain_memory(db, hooks)
                await runner.drain_proposals(db, proposal_sink, outcome.run_id, hooks)
                failed_checks = await runner.verify(environment, recorder, outcome, hooks)
                if failed_checks:
                    error = f"verifier: {failed_checks} проверок не прошли"
                    refreshed = await recorder.get_run(outcome.run_id)
                    if refreshed is not None:
                        await recorder.finish_run(refreshed, RunState.FAILED, error=error)
                    return replace(outcome, state=RunState.FAILED, error=error)
                return outcome
            finally:
                await environment.cleanup()
                await _close_backends(backends)

        return await self.with_db(action)

    async def verify(
        self,
        environment: ExecutionEnvironment,
        recorder: TraceRecorder,
        outcome: RunOutcome,
        hooks: RunHooks,
    ) -> int:
        """Детерминированный verifier после completed-run (§6.11); пишет CheckResult."""
        if outcome.state is not RunState.COMPLETED:
            return 0
        run = await recorder.get_run(outcome.run_id)
        if run is None:
            return 0
        cfg = self._cfg
        scan = scan_skills(skills_dirs(cfg, self._workspace))
        loaded = await recorder.loaded_skill_names(run)
        checks = [*cfg.verifier.checks, *skill_checks(scan.skills, loaded)]
        if not checks and not cfg.verifier.secret_scan:
            return 0
        verifier = Verifier(environment, self._workspace)
        outcomes = await verifier.run(
            checks, secret_scan=cfg.verifier.secret_scan, known_values=self.known_secret_values()
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
        return len(failed)

    async def drain_memory(self, db: AsyncSession, hooks: RunHooks) -> None:
        """Применить очередь заявок памяти single writer'ом после run (ADR-0004)."""
        mem_dir = memory_dir(self._cfg)
        if mem_dir is None or not mem_dir.is_dir():
            return
        writer = MemoryWriter(
            db, mem_dir, lock=self._lock, index_max_lines=self._cfg.memory.index_max_lines
        )
        # known_values обязательны: без них secret scan не поймает реальные
        # значения секретов, пересказанные агентом в remember (ADR-0006).
        for row in await writer.drain(known_values=self.known_secret_values()):
            if hooks.on_memory is not None:
                hooks.on_memory(row.commit_sha, row.error)

    async def drain_proposals(
        self,
        db: AsyncSession,
        sink: list[SkillProposalRequest],
        run_id: str,
        hooks: RunHooks,
    ) -> None:
        """Материализовать skill proposals в ветках skills-репозитория (Flow B, §18)."""
        if not sink:
            return
        skills_dir = self._proposals_dir()
        if skills_dir is None:
            return
        manager = SkillProposalManager(db, skills_dir)
        for request in sink:
            row = await manager.persist(
                replace(request, source_run_id=run_id), known_values=self.known_secret_values()
            )
            if hooks.on_proposal is not None:
                hooks.on_proposal(row)

    def _proposals_dir(self) -> Path | None:
        """Каталог skills для proposals — первый настроенный путь (project ./skills)."""
        dirs = skills_dirs(self._cfg, self._workspace)
        return dirs[0] if dirs else None

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
