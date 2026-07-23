"""Сборка исполнителя run'а: окружение, executor, реестр tools, мост.

Вынесено из TaskRunner (orchestrator.py): эти методы не трогают состояние
run'а — только конструируют объекты из конфига, поэтому живут отдельно от
исполняющей и пост-обрабатывающей частей.

Здесь же контракт наблюдения за прогоном (RunHooks, RunProfile): сборка
принимает его параметром, а держать типы в orchestrator.py нельзя — получился
бы цикл импорта. orchestrator.py их реэкспортирует, поэтому внешние
`from svarog_harness.runtime.orchestrator import RunHooks` продолжают работать.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.paths import first_existing_skills_dir, memory_dir, skills_dirs
from svarog_harness.config.schema import AutonomyMode, SvarogConfig
from svarog_harness.gitflow import GitRepo, WorkspaceFlow, WorkspacePrep
from svarog_harness.llm.openai_compatible import default_provider
from svarog_harness.mcp import MCPTool
from svarog_harness.memory import MemoryProposalRequest, read_memory
from svarog_harness.policy import PolicyEngine, load_policy_rules
from svarog_harness.runtime.agent_infra import ExternalAgentInfra
from svarog_harness.runtime.agents import adapter_for
from svarog_harness.runtime.bridge_control import BridgeControl
from svarog_harness.runtime.config_snapshot import config_digest
from svarog_harness.runtime.external import ExternalAgentExecutor
from svarog_harness.runtime.loop import AgentLoop
from svarog_harness.sandbox import ExecutionEnvironment, SandboxError, create_environment
from svarog_harness.secrets import SecretStore, injected_env, selected_values
from svarog_harness.skills import Skill, scan_skills, skill_cards
from svarog_harness.skills.proposal import SkillProposalRequest
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import Approval, MemoryProposal, Run, SkillProposal
from svarog_harness.tools.approval import RequestApprovalTool
from svarog_harness.tools.child_tools import SpawnChildCallback, SpawnChildRunTool
from svarog_harness.tools.file_tools import file_tools
from svarog_harness.tools.memory_tools import ProposeMemoryChangeTool, ReadMemoryTool, RememberTool
from svarog_harness.tools.plan_tools import UpdatePlanTool
from svarog_harness.tools.registry import LoadToolTool, ToolRegistry
from svarog_harness.tools.schedule_tools import ScheduleRequest, ScheduleTaskTool
from svarog_harness.tools.shell import BashTool
from svarog_harness.tools.skill_tools import CreateSkillProposalTool, ReadSkillTool
from svarog_harness.tools.user_tools import AskUserTool
from svarog_harness.trace.recorder import TraceRecorder
from svarog_harness.verifier import CheckOutcome


class RunProfile(StrEnum):
    """Набор инструментов, доступный run'у.

    DREAM — единственный run, который запускается без человека И обрабатывает
    содержимое, попавшее в память из внешних источников (sources/, заметки
    прошлых run'ов). Дать ему shell и файловые tools означало бы, что текст в
    памяти управляет исполнением; профиль закрывает это структурно, а не
    настройкой, которую можно перепутать.
    """

    DEFAULT = "default"
    DREAM = "dream"


# Автогейт deferred-схем (ADR-0015 фаза 2) при mcp.defer_schemas="auto":
# от этого числа MCP-tools их схемы уходят за load_tool.
_DEFER_SCHEMAS_AUTO_THRESHOLD = 10


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
    on_progress: Callable[[int, int, float, float, int], None] | None = None
    on_check: Callable[[CheckOutcome], None] | None = None
    on_verify_failed: Callable[[int], None] | None = None
    on_commit: Callable[[str, str, bool], None] | None = None
    on_commit_blocked: Callable[[str], None] | None = None
    on_memory: Callable[[str | None, str | None], None] | None = None
    on_proposal: Callable[[SkillProposal], None] | None = None
    on_memory_proposal: Callable[[MemoryProposal], None] | None = None
    # Живой промпт решения approval/ask_user во время гейта (§7): вызывается
    # в worker-потоке (может блокироваться на stdin) и сам записывает решение
    # в БД — poll-цикл гейта подхватит его без suspend. None — только notify,
    # решение асинхронно через `svarog approvals` или suspend→resume.
    on_approval_requested: Callable[[Approval], None] | None = None


def _approval_prompt_async(
    handler: Callable[[Approval], None] | None,
) -> Callable[[Approval], Awaitable[None]] | None:
    """Обернуть блокирующий промпт решения в worker-поток для bridge (§7).

    Промпт читает stdin и пишет решение в БД собственной короткой сессией —
    как `svarog approvals` из второго терминала, только внутри процесса.
    """
    if handler is None:
        return None

    async def prompt(approval: Approval) -> None:
        # Best-effort: сбой промпта (EOF stdin, Ctrl+C в prompt) не роняет
        # гейт — решение остаётся доступным через `svarog approvals`.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(handler, approval)

    return prompt


class RunAssembly:
    """Конструирует объекты run'а из конфига; состояния run'а не держит.

    Поля — те же объекты, что у TaskRunner (конфиг после клампа по роли,
    workspace и два скоупа секретов). TaskRunner неизменяем после
    конструктора, поэтому общие ссылки разойтись не могут.
    """

    def __init__(
        self,
        cfg: SvarogConfig,
        workspace: Path,
        *,
        store: SecretStore,
        host_store: SecretStore,
    ) -> None:
        self._cfg = cfg
        self._workspace = workspace
        self._store = store
        self._host_store = host_store

    async def with_db[T](self, action: Callable[[AsyncSession], Awaitable[T]]) -> T:
        init_db(self._cfg.storage.db_path)
        engine = create_engine(self._cfg.storage.db_path.expanduser())
        try:
            factory = create_session_factory(engine)
            async with factory() as db:
                return await action(db)
        finally:
            await engine.dispose()

    def build_environment(self, infra: ExternalAgentInfra | None = None) -> ExecutionEnvironment:
        sandbox_cfg = self._cfg.sandbox
        if self._cfg.executor.type == "external" and self._cfg.executor.external is not None:
            # Внешний агент живёт в образе с установленным агентом; версия
            # пинится тегом (ADR-0016 §8 — дрейф CLI-контрактов).
            sandbox_cfg = sandbox_cfg.model_copy(
                update={"image": self._cfg.executor.external.image}
            )
        env = injected_env(self._store, self._cfg.secrets.inject)  # только явно выданные (§12)
        if infra is not None:
            # base_url/токен bridge (ADR-0016 §3): ключа провайдера тут НЕТ.
            env = {**env, **infra.agent_env()}
        return create_environment(
            sandbox_cfg,
            self._workspace,
            skills_dir=first_existing_skills_dir(self._cfg, self._workspace),
            env=env,
            network=infra.network_name if infra is not None else None,
            extra_mounts=infra.extra_mounts if infra is not None else None,
        )

    def build_agent_infra(self) -> ExternalAgentInfra:
        """Инфраструктура run'а внешнего агента (ADR-0016 §2-§5)."""
        external = self._cfg.executor.external
        assert external is not None  # валидатор ExecutorConfig гарантирует секцию
        return ExternalAgentInfra(
            external,
            self._cfg.runtime,
            adapter_for(external),
            self._host_store,  # ключ провайдера резолвится host-side (§3)
            state_root=self._cfg.storage.db_path.expanduser().parent,
            docker_mode=self._cfg.sandbox.type == "docker",
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
        # Секреты внешнего агента (ADR-0016): ключ провайдера и OAuth-токен
        # подписки редактируются из trace и tool-выводов.
        external = self._cfg.executor.external
        if external is not None:
            if external.api_key_ref is not None:
                refs.append(external.api_key_ref)
            if external.oauth_token_ref is not None:
                refs.append(external.oauth_token_ref)
        # Redaction покрывает оба скоупа: host-store перечисляет тот же файл, а
        # selected_values добавляет env-backed refs (provider-ключ и пр.).
        return self._host_store.values() | selected_values(self._host_store, refs)

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
            schedule_sink,
            mem_dir=mem_dir,
            mcp_tools=mcp_tools,
            child_spawn=child_spawn,
            memory_proposal_sink=memory_proposal_sink,
            profile=profile,
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
            on_progress=hooks.on_progress,
            parent_run_id=parent_run_id,
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
        """Data-plane «внешний агент» (ADR-0016): адаптер + sandbox + общий trace."""
        external = self._cfg.executor.external
        assert external is not None  # валидатор ExecutorConfig гарантирует секцию
        adapter = infra.adapter if infra is not None else adapter_for(external)

        def on_run_started(run: Run) -> None:
            # Control-plane узнаёт run сразу: approvals/память привязываются к нему.
            if control is not None:
                control.set_run(run)
            if hooks.on_run_started is not None:
                hooks.on_run_started(run)

        return ExternalAgentExecutor(
            adapter,
            environment,
            recorder,
            workspace=self._workspace,
            timeout_sec=float(external.timeout_sec),
            config_hash=config_digest(self._cfg, self._workspace),  # снимок (§0.4)
            secret_values=self.known_secret_values(),
            on_text_delta=hooks.on_text_delta,
            on_tool_call=hooks.on_tool_call,
            on_run_started=on_run_started,
            on_progress=hooks.on_progress,
            parent_run_id=parent_run_id,
            bridge=infra.bridge if infra is not None else None,
            tool_output_limit=self._cfg.runtime.tool_output_context_chars,
            mcp_config=infra.mcp_config_path if infra is not None else None,
            settings_file=infra.settings_path if infra is not None else None,
            suspend_signal=control,
        )

    def wire_bridge_control(
        self,
        infra: ExternalAgentInfra,
        autonomy: AutonomyMode,
        proposal_sink: list[SkillProposalRequest],
        hooks: RunHooks,
    ) -> BridgeControl:
        """Control-plane bridge (ADR-0016 §4/§6): MCP-tools + hook-мост.

        Policy и режим автономии замораживаются здесь, при старте run
        (ADR-0010) — hook-мост не перечитывает их во время исполнения.
        """
        cfg, workspace = self._cfg, self._workspace
        external = cfg.executor.external
        assert external is not None
        policy = PolicyEngine(
            autonomy=autonomy,
            policies=cfg.policies,
            workspace=workspace,
            rules=load_policy_rules(workspace),
            skills_dirs=skills_dirs(cfg, workspace),
        )
        control = BridgeControl(
            db_action=self.with_db,
            policy=policy,
            memory_dir=memory_dir(cfg),
            skills=scan_skills(skills_dirs(cfg, workspace)).skills,
            proposal_sink=proposal_sink,
            secret_values=self.known_secret_values(),
            approval_grace_sec=float(external.approval_grace_sec),
            ask_user_timeout_sec=cfg.runtime.ask_user_timeout_sec,
            on_notify=hooks.on_notify,
            on_approval_prompt=_approval_prompt_async(hooks.on_approval_requested),
            self_docs=external.self_docs,
        )
        assert infra.bridge is not None
        infra.bridge.control_handlers.update(control.handlers())
        return control

    def prepare_agent_launch(self, infra: ExternalAgentInfra) -> None:
        """Контекст и launch-файлы агента (ADR-0016 §4/§6) до старта контейнера."""
        cfg, workspace = self._cfg, self._workspace
        external = cfg.executor.external
        assert external is not None
        mem_dir = memory_dir(cfg)
        memory_text = (
            read_memory(mem_dir, limit_bytes=cfg.memory.context_limit_bytes) or ""
            if mem_dir is not None
            else ""
        )
        cards = skill_cards(scan_skills(skills_dirs(cfg, workspace)).skills)
        infra.prepare_launch(memory_text, cards, cooperative=external.enforcement == "cooperative")

    def assert_external_autonomy_supported(self, autonomy: AutonomyMode) -> None:
        """Supervised/auto с внешним агентом требует tier 2 (fail-closed, §6)."""
        external = self._cfg.executor.external
        if self._cfg.executor.type != "external" or external is None:
            return
        if autonomy is AutonomyMode.YOLO:
            return
        adapter = adapter_for(external)
        if external.enforcement != "cooperative" or not adapter.capabilities().hooks:
            raise SandboxError(
                f"режим '{autonomy.value}' с внешним агентом требует "
                "executor.external.enforcement='cooperative' и адаптера с hook-поддержкой "
                "(fail-closed, ADR-0016 §6): tier 1 не даёт per-tool контроля"
            )

    def _defer_mcp_schemas(self, mcp_tools: list[MCPTool] | None) -> bool:
        """Автогейт deferred-схем (ADR-0015 фаза 2): "auto" → 10+ MCP-tools."""
        flag = self._cfg.mcp.defer_schemas
        if flag == "auto":
            return len(mcp_tools or []) >= _DEFER_SCHEMAS_AUTO_THRESHOLD
        return bool(flag)

    def _build_registry(
        self,
        environment: ExecutionEnvironment,
        skills: list[Skill],
        skill_load_sink: list[tuple[str, str | None]],
        memory_sink: list[dict[str, object]],
        plan_update_sink: list[dict[str, object]],
        proposal_sink: list[SkillProposalRequest] | None,
        schedule_sink: list[ScheduleRequest] | None,
        *,
        mem_dir: Path | None,
        mcp_tools: list[MCPTool] | None = None,
        child_spawn: SpawnChildCallback | None = None,
        memory_proposal_sink: list[MemoryProposalRequest] | None = None,
        profile: RunProfile = RunProfile.DEFAULT,
    ) -> ToolRegistry:
        registry = ToolRegistry()
        if profile is RunProfile.DREAM:
            # Только чтение памяти и предложение правок; всё остальное — включая
            # remember — не регистрируется вовсе (§6 спеки блока C).
            if mem_dir is not None:
                registry.register(ReadMemoryTool(mem_dir))
                if memory_proposal_sink is not None:
                    registry.register(
                        ProposeMemoryChangeTool(
                            on_propose=memory_proposal_sink.append, memory_dir=mem_dir
                        )
                    )
            return registry
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
        if child_spawn is not None:
            # Child runs (ADR-0015 фаза 3): только верхнеуровневым runs — детям
            # callback не передаётся, глубина дерева ограничена одним уровнем.
            registry.register(SpawnChildRunTool(child_spawn))
        defer_schemas = self._defer_mcp_schemas(mcp_tools)
        for mcp_tool in mcp_tools or []:
            # MCP tools проходят через Policy Engine как обычные (§9): по умолчанию
            # require_approval (action_type mcp.*), риск из конфига сервера.
            # При defer_schemas (ADR-0015 фаза 2) схема в промпт не грузится,
            # пока модель не вызовет load_tool.
            # external: MCP-схемы дописываются после встроенных, чтобы смена
            # discovery не сдвигала кэшируемый префикс промпта (блок A §3).
            registry.register(mcp_tool, deferred=defer_schemas, external=True)
        if defer_schemas:
            registry.register(LoadToolTool(registry))
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
        if schedule_sink is not None:
            # schedule.create — неотключаемый critical-набор (ADR-0010):
            # approval требуется в любом режиме автономии.
            registry.register(ScheduleTaskTool(on_enqueue=schedule_sink.append))
        if proposal_sink is not None:
            # Skill governance (Flow B, §18): агент предлагает скиллы через proposal,
            # прямые правки skills/ запрещены policy.
            registry.register(CreateSkillProposalTool(on_propose=proposal_sink.append))
        return registry
