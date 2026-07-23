"""Оркестрация одного прогона задачи (§6.2, §11) — общая для всех интерфейсов.

CLI, gateway (REST/WS) и Telegram гоняют один и тот же end-to-end цикл:
подготовка workspace (Flow C) → sandbox → agent loop → слив памяти (Flow A)
→ verifier → auto-commit. Раньше он жил в `cli/main.py` вперемешку с выводом
в консоль; вынесен сюда, а весь пользовательский вывод идёт через `RunHooks`.
Так gateway не дублирует ядро (repo-structure: `cli`/`gateway` → `runtime`).
"""

import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.loader import load_config
from svarog_harness.config.paths import (
    assert_workspace_isolated,
    clamp_by_role,
    memory_dir,
    skills_dirs,
    workspace_layout_violations,
)
from svarog_harness.config.schema import AutonomyMode, SvarogConfig, TenantRole
from svarog_harness.gitflow import GitRepo, SecretScanBlockedError, WorkspaceFlow, WorkspacePrep
from svarog_harness.llm.provider import ChatMessage
from svarog_harness.mcp import MCPBackend, MCPTool, build_mcp_tools, connect_mcp_servers
from svarog_harness.memory import (
    MemoryProposalManager,
    MemoryProposalRequest,
    MemoryWriter,
)
from svarog_harness.runtime.agent_infra import ExternalAgentInfra
from svarog_harness.runtime.bridge_control import BridgeControl
from svarog_harness.runtime.checkpoint import LoopState
from svarog_harness.runtime.config_snapshot import CONFIG_HASH_META_KEY, config_digest
from svarog_harness.runtime.external import (
    AGENT_SESSION_META_KEY,
    EXECUTOR_META_KEY,
    ExternalAgentExecutor,
)
from svarog_harness.runtime.loop import AgentLoop, RunOutcome
from svarog_harness.runtime.run_assembly import RunAssembly

# Реэкспорт контракта наблюдения: типы переехали в run_assembly (иначе цикл
# импорта), но исторический путь `from ...orchestrator import RunHooks`
# используется в cli/gateway и остаётся рабочим. Форма `X as X` — явный
# реэкспорт для mypy strict.
from svarog_harness.runtime.run_assembly import RunHooks as RunHooks
from svarog_harness.runtime.run_assembly import RunProfile as RunProfile
from svarog_harness.sandbox import (
    ExecutionEnvironment,
    SandboxError,
    find_docker,
)
from svarog_harness.scheduler.schedule import next_run_after
from svarog_harness.scheduler.store import JobStore
from svarog_harness.secrets import (
    SecretStore,
    default_secret_store,
    redact,
)
from svarog_harness.skills import scan_skills
from svarog_harness.skills.curator import CuratorStore
from svarog_harness.skills.proposal import SkillProposalRequest
from svarog_harness.skills.proposal_manager import SkillProposalManager
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.locks import LockBackend, default_lock_backend
from svarog_harness.storage.models import (
    ApprovalStatus,
    JobOrigin,
    Run,
    RunState,
    utcnow,
)
from svarog_harness.tools.base import ToolError
from svarog_harness.tools.child_tools import (
    SpawnChildCallback,
    SpawnChildRunArgs,
)
from svarog_harness.tools.schedule_tools import ScheduleRequest
from svarog_harness.trace.lookup import RunNotResumableError, find_run_by_prefix
from svarog_harness.trace.recorder import TraceRecorder
from svarog_harness.verifier import Verifier, skill_checks


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


def _deadline_passed(deadline_iso: str) -> bool:
    """Дедлайн ask_user (§6.5) истёк? Невалидная строка — считаем истёкшим."""
    from datetime import datetime

    try:
        deadline = datetime.fromisoformat(deadline_iso)
    except ValueError:
        return True
    return utcnow() >= deadline


async def _close_backends(backends: list[MCPBackend]) -> None:
    for backend in backends:
        with contextlib.suppress(Exception):
            await backend.close()


@dataclass
class SessionResources:
    """Тёплый sandbox серии runs одной сессии (ADR-0017, gateway-chat).

    Владелец — сессия gateway, а не отдельный run: run_once с resources не
    строит и не убирает env/infra/MCP, экономя старт контейнера на каждое
    сообщение. Для внешнего агента bridge (и его budget) живёт всю серию —
    семантика CLI-chat.
    """

    environment: ExecutionEnvironment
    infra: ExternalAgentInfra | None
    backends: list[MCPBackend]

    async def close(self) -> None:
        """Идемпотентно закрыть всё; ошибки одного шага не блокируют остальные."""
        with contextlib.suppress(Exception):
            await self.environment.cleanup()
        await _close_backends(self.backends)
        if self.infra is not None:
            with contextlib.suppress(Exception):
                await self.infra.stop()


class TaskRunner:
    """Гоняет задачи в фиксированном workspace по одной конфигурации."""

    def __init__(
        self,
        cfg: SvarogConfig,
        workspace: Path,
        *,
        role: TenantRole = TenantRole.SUPERUSER,
        allow_layout_overlap: bool = False,
    ) -> None:
        # Роль фиксируется при старте (ADR-0010/0013) и заклампывает cfg
        # идемпотентно: standard → docker/hardened, superuser (по умолчанию) —
        # no-op. Клампим здесь, чтобы гарантия держалась и на resume (см. resume).
        self._role = role
        self._cfg = clamp_by_role(cfg, role)
        cfg = self._cfg
        self._workspace = workspace
        # Подтверждённое человеком пересечение workspace с control-plane
        # (ADR-0018, только локальный CLI): для standard-тенантов клампится в
        # False — гейт раскладки ADR-0015 §0.3 для них безусловный.
        self._allow_layout_overlap = allow_layout_overlap and role is TenantRole.SUPERUSER
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
        # Сборка исполнителя вынесена (run_assembly.py): она не трогает
        # состояние run'а, только конструирует объекты из конфига.
        self._assembly = RunAssembly(
            self._cfg, workspace, store=self._store, host_store=self._host_store
        )

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
        # Внешний агент (ADR-0016 §2): границу безопасности держит только
        # sandbox-периметр, поэтому local-trusted для него не существует.
        if self._cfg.executor.type == "external" and self._cfg.sandbox.type != "docker":
            raise SandboxError(
                "executor.type='external' требует sandbox.type='docker' (fail-closed, "
                "ADR-0016): внешний агент исполняется только внутри контейнера"
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

    # --- сборка исполнителя (делегируется RunAssembly, run_assembly.py) ------
    # Публичные сигнатуры сохранены: cli/chat_engine.py и тесты зовут их у
    # TaskRunner.

    def build_environment(self, infra: ExternalAgentInfra | None = None) -> ExecutionEnvironment:
        return self._assembly.build_environment(infra)

    def build_agent_infra(self) -> ExternalAgentInfra:
        return self._assembly.build_agent_infra()

    def known_secret_values(self) -> frozenset[str]:
        return self._assembly.known_secret_values()

    def build_loop(
        self,
        recorder: TraceRecorder,
        environment: ExecutionEnvironment,
        autonomy: AutonomyMode,
        hooks: RunHooks,
        proposal_sink: list[SkillProposalRequest] | None = None,
        schedule_sink: list[ScheduleRequest] | None = None,
        *,
        excluded_skills: frozenset[str] = frozenset(),
        mcp_tools: list[MCPTool] | None = None,
        child_spawn: SpawnChildCallback | None = None,
        parent_run_id: str | None = None,
        memory_proposal_sink: list[MemoryProposalRequest] | None = None,
        profile: RunProfile = RunProfile.DEFAULT,
    ) -> AgentLoop:
        return self._assembly.build_loop(
            recorder,
            environment,
            autonomy,
            hooks,
            proposal_sink,
            schedule_sink,
            excluded_skills=excluded_skills,
            mcp_tools=mcp_tools,
            child_spawn=child_spawn,
            parent_run_id=parent_run_id,
            memory_proposal_sink=memory_proposal_sink,
            profile=profile,
        )

    def build_external_executor(
        self,
        recorder: TraceRecorder,
        environment: ExecutionEnvironment,
        hooks: RunHooks,
        *,
        parent_run_id: str | None = None,
        infra: ExternalAgentInfra | None = None,
        control: BridgeControl | None = None,
    ) -> ExternalAgentExecutor:
        return self._assembly.build_external_executor(
            recorder,
            environment,
            hooks,
            parent_run_id=parent_run_id,
            infra=infra,
            control=control,
        )

    def wire_bridge_control(
        self,
        infra: ExternalAgentInfra,
        autonomy: AutonomyMode,
        proposal_sink: list[SkillProposalRequest],
        hooks: RunHooks,
    ) -> BridgeControl:
        return self._assembly.wire_bridge_control(infra, autonomy, proposal_sink, hooks)

    def prepare_agent_launch(self, infra: ExternalAgentInfra) -> None:
        self._assembly.prepare_agent_launch(infra)

    def assert_external_autonomy_supported(self, autonomy: AutonomyMode) -> None:
        self._assembly.assert_external_autonomy_supported(autonomy)

    # --- исполнение ---------------------------------------------------------

    async def recover(self, recorder: TraceRecorder, hooks: RunHooks) -> None:
        """Recovery незавершённых runs при старте (ADR-0005)."""
        for run in await recorder.recover_interrupted_runs():
            if hooks.on_recovered is not None:
                hooks.on_recovered(run)

    async def spawn_child_run(
        self,
        recorder: TraceRecorder,
        parent_run: Run,
        autonomy: AutonomyMode,
        args: SpawnChildRunArgs,
        hooks: RunHooks,
        *,
        excluded_skills: frozenset[str] = frozenset(),
    ) -> str:
        """Дочерний run (ADR-0015 фаза 3): worktree → клампнутый бюджет → loop.

        Ребёнок — обычный `Run` с `parent_run_id`, своим checkpoint'ом и
        config-snapshot'ом. Результат durable в той же SQLite: повторный spawn
        той же подзадачи (write-ahead resume родителя) вернёт результат из
        trace, не гоняя ребёнка заново. Работа ребёнка коммитится на его
        ветке; физический worktree после успеха убирается.
        """
        # Lookup по той же redacted-форме, в которой loop сохраняет Run.task.
        task = redact(args.task, self.known_secret_values())
        existing = await recorder.find_completed_child_run(parent_run.id, task)
        if existing is not None:
            answer = await recorder.last_assistant_text(existing)
            return f"дочерний run {existing.id[:8]} уже выполнен (результат из trace):\n{answer}"

        parent_repo = GitRepo(self._workspace)
        if not await parent_repo.is_repo() or not await parent_repo.has_commits():
            raise ToolError(
                "spawn_child_run требует git-workspace хотя бы с одним коммитом: "
                "изоляция ребёнка — отдельный git-worktree (ADR-0015 фаза 3)"
            )
        # Делегация внешнему агенту (ADR-0016 фаза 3.5): data-plane ребёнка —
        # ExternalAgentExecutor; секция external должна быть настроена заранее.
        delegate = args.executor == "external"
        if delegate and self._cfg.executor.external is None:
            raise ToolError(
                "делегация внешнему агенту (executor='external') требует секции "
                "executor.external в svarog.yaml (ADR-0016 фаза 3.5); выполните "
                "подзадачу нативно или попросите оператора настроить секцию"
            )
        suffix = uuid.uuid4().hex[:8]
        branch = f"svarog/child-{suffix}"
        ws = self._workspace.expanduser().resolve()
        # Worktree — сосед workspace (вне его дерева и вне bind-mount родителя),
        # по образцу .gitdirs из §0.2.
        child_ws = ws.parent / ".worktrees" / f"{ws.name}-{suffix}"
        await parent_repo.add_worktree(child_ws, branch)

        # Бюджеты клампятся вниз к родительским (как autonomy/role): запросить
        # больше, чем разрешено родителю, нельзя.
        runtime = self._cfg.runtime
        child_runtime = runtime.model_copy(
            update={
                "max_iterations": min(
                    args.max_iterations or runtime.max_iterations, runtime.max_iterations
                ),
                "max_tokens_per_run": min(
                    args.max_tokens or runtime.max_tokens_per_run, runtime.max_tokens_per_run
                ),
                "max_cost_usd_per_run": min(
                    args.max_cost_usd or runtime.max_cost_usd_per_run,
                    runtime.max_cost_usd_per_run,
                ),
            }
        )
        child_cfg = self._cfg.model_copy(update={"runtime": child_runtime})
        if delegate:
            child_cfg = child_cfg.model_copy(
                update={"executor": self._cfg.executor.model_copy(update={"type": "external"})}
            )
        child_runner = TaskRunner(child_cfg, child_ws, role=self._role)
        child_infra: ExternalAgentInfra | None = None
        if delegate:
            # Fail-closed гейты (docker-only §2, supervised §6) возвращаются
            # модели tool-ошибкой: родитель может выполнить подзадачу нативно,
            # а не падать целиком; свежесозданный worktree убираем.
            try:
                child_runner.assert_sandbox_available()
                child_runner.assert_external_autonomy_supported(autonomy)
            except SandboxError as exc:
                with contextlib.suppress(Exception):
                    await parent_repo.remove_worktree(child_ws)
                raise ToolError(str(exc)) from exc
        else:
            child_runner.assert_sandbox_available()
        # Свой lease на своё дерево (§0.5): parent и child не конфликтуют.
        await recorder.acquire_workspace_lease(str(child_ws))
        if delegate:
            child_infra = child_runner.build_agent_infra()
        environment: ExecutionEnvironment | None = None
        child_proposals: list[SkillProposalRequest] = []
        try:
            if child_infra is not None:
                # start/prepare внутри try: сбой не осиротит bridge/сеть ребёнка.
                await child_infra.start()
                child_runner.prepare_agent_launch(child_infra)
            environment = child_runner.build_environment(child_infra)
            await environment.start()
            # Ребёнок не стримит текст в канал родителя (перемешался бы с его
            # выводом); tool calls и notify пробрасываются для наблюдаемости.
            child_hooks = RunHooks(on_tool_call=hooks.on_tool_call, on_notify=hooks.on_notify)
            if delegate:
                assert child_infra is not None
                control = child_runner.wire_bridge_control(
                    child_infra, autonomy, child_proposals, child_hooks
                )
                outcome = await child_runner.build_external_executor(
                    recorder,
                    environment,
                    child_hooks,
                    parent_run_id=parent_run.id,
                    infra=child_infra,
                    control=control,
                ).run(args.task, autonomy)
            else:
                loop = child_runner.build_loop(
                    recorder,
                    environment,
                    autonomy,
                    child_hooks,
                    None,
                    excluded_skills=excluded_skills,
                    mcp_tools=None,
                    parent_run_id=parent_run.id,
                )
                outcome = await loop.run(args.task, autonomy)
        finally:
            if environment is not None:
                await environment.cleanup()
            if child_infra is not None:
                await child_infra.stop()

        if child_proposals:
            # Proposals делегированного ребёнка — тот же Flow B, что у родителя.
            async def _drain_child_proposals(db: AsyncSession) -> None:
                await child_runner.drain_proposals(db, child_proposals, outcome.run_id, hooks)

            await child_runner.with_db(_drain_child_proposals)

        committed: str | None = None
        if outcome.state is RunState.COMPLETED:
            # Работа ребёнка — на его ветке (durable); физический worktree после
            # коммита убираем. Secret-scan-блок оставит дерево грязным — тогда
            # remove откажется и worktree сохранится для разбора.
            with contextlib.suppress(Exception):
                committed = await WorkspaceFlow(GitRepo(child_ws), self._cfg.git).commit_step(
                    f"svarog child: {args.task[:64]}",
                    run_id=outcome.run_id,
                    known_values=self.known_secret_values(),
                )
            with contextlib.suppress(Exception):
                await parent_repo.remove_worktree(child_ws)
        else:
            # suspended/failed: worktree сохраняем — checkpoint ребёнка ссылается
            # на него, `svarog resume` дочернего run'а возможен.
            raise ToolError(
                f"дочерний run {outcome.run_id[:8]} завершился "
                f"'{outcome.state.value}': {outcome.error or 'без причины'}; "
                f"worktree сохранён для resume: {child_ws}"
            )

        parts = [
            f"дочерний run {outcome.run_id[:8]} выполнен (итераций: {outcome.iterations}, "
            f"токенов: {outcome.tokens_used}, стоимость: ${outcome.cost_usd:.4f})"
        ]
        if committed is not None:
            parts.append(f"изменения ребёнка — на ветке {branch} (коммит {committed})")
        parts.append(f"результат:\n{outcome.final_answer}")
        return "\n".join(parts)

    async def prepare_session_resources(self, autonomy: AutonomyMode) -> "SessionResources":
        """Тёплый sandbox для серии runs одной сессии (ADR-0017, gateway-chat).

        Те же шаги построения, что в run_once, но env/infra/MCP остаются жить
        между сообщениями — как в CLI-chat. Следствие для внешнего агента:
        budget bridge (max_tokens/cost per run) действует на всю серию, а не
        на одно сообщение — тот же trade-off, что у CLI-chat. Закрытие — на
        вызывающем (`SessionResources.close`).
        """
        self.assert_sandbox_available()  # fail-closed (ADR-0013)
        external = self._cfg.executor.type == "external"
        backends = [] if external else await connect_mcp_servers(self._cfg.mcp, self._host_store)
        infra: ExternalAgentInfra | None = None
        environment: ExecutionEnvironment | None = None
        try:
            if external:
                self.assert_external_autonomy_supported(autonomy)  # fail-closed (§6)
                infra = self.build_agent_infra()
                await infra.start()
                self.prepare_agent_launch(infra)
            environment = self.build_environment(infra)
            await environment.start()
            return SessionResources(environment=environment, infra=infra, backends=backends)
        except BaseException:
            # Частично поднятое не осиротает: закрываем всё, что успели.
            if environment is not None:
                await environment.cleanup()
            await _close_backends(backends)
            if infra is not None:
                await infra.stop()
            raise

    async def run_once(
        self,
        task: str,
        autonomy: AutonomyMode,
        *,
        hooks: RunHooks,
        session_id: str | None = None,
        history: list[ChatMessage] | None = None,
        resources: "SessionResources | None" = None,
        profile: RunProfile = RunProfile.DEFAULT,
    ) -> RunOutcome:
        """Полный прогон: workspace prep → sandbox → loop → память → verifier → commit.

        session_id/history — серия runs одной сессии (§10.1): gateway-chat
        (ADR-0017 §2) гоняет каждое сообщение отдельным run'ом в общем
        workspace. Нативный loop получает history в контекст; внешний агент
        (ADR-0016) контекст диалога держит в собственной сессии — run
        продолжает её через agent_session_id предыдущего run'а Session
        (history для него игнорируется, как в CLI-chat).

        resources — тёплый sandbox сессии (prepare_session_resources): env/
        infra/MCP не строятся и не убираются этим run'ом, ими владеет сессия.
        """
        self.assert_sandbox_available()  # fail-closed до любой работы (ADR-0013)
        assert_workspace_isolated(
            self._cfg, self._workspace, allow_overlap=self._allow_layout_overlap
        )  # раскладка (ADR-0015 §0.3)
        self._warn_layout_tradeoff(hooks)
        # Fail-closed гейты внешнего агента — ДО Flow C: отказ конфигурации не
        # должен оставлять мусорную task-ветку (S15a, кампания 21.07.2026).
        if self._cfg.executor.type == "external":
            self.assert_external_autonomy_supported(autonomy)
        flow = WorkspaceFlow(GitRepo(self._workspace), self._cfg.git)
        prep = await flow.start(task)
        if hooks.on_workspace_prep is not None:
            hooks.on_workspace_prep(prep)

        external = self._cfg.executor.type == "external"
        owned = resources is None  # владеем ли env/infra/MCP этим прогоном
        # MCP внешнему агенту не пробрасывается (у него свой MCP-сервер
        # Svarog через bridge, §4): host-side серверы зря не поднимаем.
        if resources is None:
            backends = (
                [] if external else await connect_mcp_servers(self._cfg.mcp, self._host_store)
            )
            infra: ExternalAgentInfra | None = None
        else:
            backends = resources.backends  # host-скоуп
            infra = resources.infra
        environment: ExecutionEnvironment | None = None
        if owned and external:
            self.assert_external_autonomy_supported(autonomy)  # fail-closed (§6)
            infra = self.build_agent_infra()
        try:
            if resources is None:
                if infra is not None:
                    # Bridge (LLM-прокси + control) и internal-сеть — до контейнера;
                    # внутри try, чтобы сбой prepare_launch не осиротил уже поднятые
                    # bridge/сеть (finally гарантированно вызовет infra.stop()).
                    await infra.start()
                    self.prepare_agent_launch(infra)
                env = self.build_environment(infra)
                environment = env
                await env.start()
            else:
                if external:
                    self.assert_external_autonomy_supported(autonomy)  # fail-closed (§6)
                env = resources.environment
            mcp_tools = build_mcp_tools(backends)

            async def action(db: AsyncSession) -> RunOutcome:
                recorder = TraceRecorder(db)
                await self.recover(recorder, hooks)
                # Per-workspace lease (ADR-0015 §0.5): второй параллельный run на
                # том же рабочем дереве отклоняется, пока первый жив (heartbeat).
                await recorder.acquire_workspace_lease(str(self._workspace))
                proposal_sink: list[SkillProposalRequest] = []
                schedule_sink: list[ScheduleRequest] = []
                memory_proposal_sink: list[MemoryProposalRequest] = []
                if external:
                    # Data-plane — внешний агент (ADR-0016): память и скиллы
                    # доступны агенту через MCP-сервер bridge (§4).
                    assert infra is not None
                    control = self.wire_bridge_control(infra, autonomy, proposal_sink, hooks)
                    executor = self.build_external_executor(
                        recorder, env, hooks, infra=infra, control=control
                    )
                    # Chat-непрерывность (ADR-0016 фаза 3, как в CLI-chat):
                    # новый run продолжает сессию агента предыдущего run'а Session.
                    agent_session = (
                        await recorder.last_agent_session(session_id)
                        if session_id is not None
                        else None
                    )
                    outcome = await executor.run(
                        task, autonomy, session_id=session_id, agent_session=agent_session
                    )
                else:
                    excluded = frozenset(await CuratorStore(db).archived_names())
                    # Child runs (ADR-0015 фаза 3): родительский Run становится
                    # известен через on_run_started — держим его в holder'е для
                    # callback'а spawn_child_run.
                    parent_runs: list[Run] = []

                    def _on_run_started(run: Run) -> None:
                        parent_runs.append(run)
                        if hooks.on_run_started is not None:
                            hooks.on_run_started(run)

                    async def spawn(args: SpawnChildRunArgs) -> str:
                        if not parent_runs:
                            raise ToolError("родительский run ещё не зарегистрирован")
                        return await self.spawn_child_run(
                            recorder,
                            parent_runs[-1],
                            autonomy,
                            args,
                            hooks,
                            excluded_skills=excluded,
                        )

                    loop = self.build_loop(
                        recorder,
                        env,
                        autonomy,
                        replace(hooks, on_run_started=_on_run_started),
                        proposal_sink,
                        schedule_sink,
                        excluded_skills=excluded,
                        mcp_tools=mcp_tools,
                        child_spawn=spawn,
                        memory_proposal_sink=memory_proposal_sink,
                        profile=profile,
                    )
                    outcome = await loop.run(task, autonomy, session_id=session_id, history=history)
                await self.drain_memory(db, hooks)
                await self.drain_proposals(db, proposal_sink, outcome.run_id, hooks)
                await self.drain_memory_proposals(db, memory_proposal_sink, outcome.run_id, hooks)
                await self.drain_schedule(db, schedule_sink, self._workspace, hooks)
                failed_checks = await self.verify(env, recorder, outcome, hooks)
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
            if owned:
                if environment is not None:
                    await environment.cleanup()
                await _close_backends(backends)
                if infra is not None:
                    await infra.stop()

    def _runner_for_resume(self, workspace: Path) -> "TaskRunner":
        """Runner для resume в workspace checkpoint'а.

        Свой workspace — этот же runner с entry-конфигом: gateway-runs в
        per-run workspaces (ADR-0017) возобновляются под конфигом тенанта,
        а не под `svarog.yaml`, который мог приехать внутри склонированного
        репозитория (щель trust gate). Чужой workspace (CLI `svarog resume`
        для run'а из другой папки) — как раньше, конфиг workspace'а +
        re-clamp роли (ADR-0013).
        """
        ws = workspace.expanduser().resolve()
        if ws == self._workspace.expanduser().resolve():
            return self
        return TaskRunner(
            load_config(project_dir=workspace),
            workspace,
            role=self._role,
            allow_layout_overlap=self._allow_layout_overlap,
        )

    async def resume(self, run_id: str, *, hooks: RunHooks) -> RunOutcome:
        """Возобновить run из checkpoint (ADR-0005).

        БД берётся из entry-конфигурации этого runner'а, а рабочая директория
        и её runtime/sandbox-настройки — из конфига workspace'а checkpoint'а;
        режим автономии заморожен в самом run (ADR-0010).
        """

        async def action(db: AsyncSession) -> RunOutcome:
            recorder = TraceRecorder(db)
            await self.recover(recorder, hooks)
            probe = await find_run_by_prefix(db, run_id)
            if (probe.meta or {}).get(EXECUTOR_META_KEY) == "external":
                # Внешний run (ADR-0016 §7): checkpoint'а нет — сессию
                # поднимает сам агент по agent_session_id.
                return await self._resume_external(db, recorder, probe, hooks)
            run, raw_state = await recorder.load_resumable(run_id)
            state = LoopState.from_dict(raw_state)
            workspace = state.workspace
            if not workspace.is_dir():
                raise RunNotResumableError(f"workspace run'а больше не существует: {workspace}")
            # Роль тенанта переклампывает конфиг workspace'а на resume (ADR-0013):
            # standard остаётся в docker/hardened, даже если yaml workspace'а
            # говорит local-trusted. Для superuser (CLI-resume) — no-op.
            runner = self._runner_for_resume(workspace)
            runner.assert_sandbox_available()  # fail-closed на resume (ADR-0013)
            assert_workspace_isolated(
                runner._cfg, workspace, allow_overlap=runner._allow_layout_overlap
            )  # раскладка (ADR-0015 §0.3)
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
                # schedule.create — неотключаемый critical-набор (ADR-0010/0019):
                # на resume одобренный critical переигрывается и попадает в sink,
                # а drain_schedule после resume материализует его в cron_jobs.
                # Зеркало run_once (см. строки 611/661/672) — иначе approval+resume
                # даёт completed без джобы (баг S24).
                schedule_sink: list[ScheduleRequest] = []
                excluded = frozenset(await CuratorStore(db).archived_names())

                async def spawn(args: SpawnChildRunArgs) -> str:
                    # Родитель при resume известен сразу — это возобновляемый run.
                    return await runner.spawn_child_run(
                        recorder,
                        run,
                        AutonomyMode(run.autonomy),
                        args,
                        hooks,
                        excluded_skills=excluded,
                    )

                loop = runner.build_loop(
                    recorder,
                    environment,
                    AutonomyMode(run.autonomy),
                    hooks,
                    proposal_sink,
                    schedule_sink,
                    excluded_skills=excluded,
                    mcp_tools=build_mcp_tools(backends),
                    child_spawn=spawn,
                )
                outcome = await loop.resume(run, state)
                await runner.drain_memory(db, hooks)
                await runner.drain_proposals(db, proposal_sink, outcome.run_id, hooks)
                await runner.drain_schedule(db, schedule_sink, workspace, hooks)
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

    async def _resume_external(
        self, db: AsyncSession, recorder: TraceRecorder, run: Run, hooks: RunHooks
    ) -> RunOutcome:
        """Resume run'а внешнего агента (ADR-0016 §7).

        Вместо checkpoint'а — сессия агента: prompt-решение (approval /
        ответ ask_user / «лимиты подняты») инжектируется в `--resume`
        сессии; ретрай заблокированного действия пропустит decision cache
        hook-моста по отпечатку вызова.
        """
        if run.state not in (RunState.SUSPENDED, RunState.WAITING_APPROVAL):
            raise RunNotResumableError(
                f"run {run.id[:8]} в состоянии '{run.state.value}' не возобновляется"
            )
        agent_session = (run.meta or {}).get(AGENT_SESSION_META_KEY)
        if not isinstance(agent_session, str) or not agent_session:
            raise RunNotResumableError(
                f"run {run.id[:8]}: нет agent_session_id — сессию внешнего агента "
                "не восстановить (агент упал до init-события)"
            )
        workspace = Path(run.workspace or "").expanduser()
        if not workspace.is_dir():
            raise RunNotResumableError(f"workspace run'а больше не существует: {workspace}")
        runner = self._runner_for_resume(workspace)
        runner.assert_sandbox_available()
        runner.assert_external_autonomy_supported(AutonomyMode(run.autonomy))
        _assert_config_unchanged(run, runner._cfg, workspace)  # trust gate (§0.4)
        await recorder.acquire_workspace_lease(str(workspace))
        prompt = await self._external_resume_prompt(recorder, run)
        infra = runner.build_agent_infra()
        await infra.start()
        runner.prepare_agent_launch(infra)
        environment = runner.build_environment(infra)
        await environment.start()
        try:
            proposal_sink: list[SkillProposalRequest] = []
            autonomy = AutonomyMode(run.autonomy)
            control = runner.wire_bridge_control(infra, autonomy, proposal_sink, hooks)
            control.set_run(run)
            executor = runner.build_external_executor(
                recorder, environment, hooks, infra=infra, control=control
            )
            outcome = await executor.resume(run, prompt, agent_session=agent_session)
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
            await infra.stop()

    async def _external_resume_prompt(self, recorder: TraceRecorder, run: Run) -> str:
        """Prompt-решение для сессии агента по последнему approval run'а."""
        if run.state is RunState.SUSPENDED:
            return (
                "Run был приостановлен по лимиту бюджета; лимиты подняты. "
                "Продолжай прерванную задачу с того места, где остановился."
            )
        approval = await recorder.latest_approval(run.id)
        if approval is None:
            return "Продолжай прерванную задачу."
        if approval.status is ApprovalStatus.PENDING:
            deadline = str(approval.payload.get("deadline", ""))
            if approval.action_type == "user.question" and _deadline_passed(deadline):
                await recorder.expire_approval(approval)
                return (
                    "Ответа человека на твой вопрос нет (таймаут истёк). "
                    "Продолжай по своему усмотрению."
                )
            raise RunNotResumableError(
                f"решение по approval {approval.id[:8]} ещё не принято: "
                f"svarog approvals approve/deny/answer {approval.id[:8]}"
            )
        if approval.action_type == "user.question":
            answer = (approval.reason or "").strip()
            return (
                f"Ответ пользователя: {answer}. Продолжай задачу с учётом ответа."
                if answer
                else "Пользователь не дал ответа по существу; продолжай по своему усмотрению."
            )
        if approval.status is ApprovalStatus.APPROVED:
            return (
                "Approval получен — действие одобрено человеком. Повтори "
                "заблокированное действие (решение закэшировано) и продолжай задачу."
            )
        reason = approval.reason or "без причины"
        return (
            f"Действие отклонено человеком: {reason}. НЕ повторяй его; "
            "продолжай задачу с учётом отказа или заверши с объяснением."
        )

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

    async def drain_schedule(
        self,
        db: AsyncSession,
        requests: list[ScheduleRequest],
        workspace: Path,
        hooks: RunHooks,
    ) -> None:
        """Материализовать одобренные заявки на джобы (ADR-0019, блок D §7).

        Заявка сюда попадает только если инструмент реально исполнился, то есть
        approval уже получен: `schedule.create` — неотключаемый critical-набор.
        Поэтому джоба заводится сразу включённой; права замораживаются здесь —
        автономия наследуется от run'а, дайджест конфига берётся текущий.
        """
        if not requests:
            return
        store = JobStore(db)
        for request in requests:
            first = next_run_after(request.kind, request.spec, request.tz, utcnow())
            job = await store.create(
                name=request.name,
                kind=request.kind,
                spec=request.spec,
                tz=request.tz,
                task=request.task,
                workspace=str(workspace),
                autonomy=self._cfg.runtime.autonomy.value,
                config_digest=config_digest(self._cfg, workspace),
                origin=JobOrigin.AGENT,
                first_run_at=first,
            )
            await store.set_enabled(job, True)
            if hooks.on_notify is not None:
                hooks.on_notify("schedule", f"джоба «{job.name}» создана и включена ({job.id[:8]})")
        requests.clear()

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

    async def drain_memory_proposals(
        self,
        db: AsyncSession,
        sink: list[MemoryProposalRequest],
        run_id: str,
        hooks: RunHooks,
    ) -> None:
        """Материализовать предложения правок памяти (блок C, ADR-0020)."""
        if not sink:
            return
        mem_dir = memory_dir(self._cfg)
        if mem_dir is None or not mem_dir.is_dir():
            return
        manager = MemoryProposalManager(db, mem_dir)
        for request in sink:
            row = await manager.persist(replace(request, source_run_id=run_id))
            if hooks.on_memory_proposal is not None:
                hooks.on_memory_proposal(row)

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
