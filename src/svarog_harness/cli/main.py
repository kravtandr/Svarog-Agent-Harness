"""Точка входа CLI `svarog` (§10.1).

Команды init/chat/skills добавляются по мере milestones (см.
docs/first-issues.md); после M2 доступны run, resume, traces list/show,
approvals list/approve/deny, version.
"""

import asyncio
import contextlib
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness import __version__
from svarog_harness.config.loader import ConfigError, load_config
from svarog_harness.config.schema import AutonomyMode, SvarogConfig
from svarog_harness.gitflow import (
    GitError,
    GitRepo,
    SecretScanBlockedError,
    WorkspaceFlow,
    WorkspacePrep,
)
from svarog_harness.llm.openai_compatible import ApiKeyError, default_provider
from svarog_harness.memory import MemoryWriter, read_memory
from svarog_harness.policy import PolicyEngine, PolicyRulesError, load_policy_rules
from svarog_harness.policy.engine import PolicyAction
from svarog_harness.runtime.checkpoint import LoopState
from svarog_harness.runtime.loop import AgentLoop, RunOutcome
from svarog_harness.sandbox import ExecutionEnvironment, SandboxError, create_environment
from svarog_harness.scaffold import scaffold_agent_home
from svarog_harness.secrets import (
    FileSecretStore,
    SecretStore,
    default_secret_store,
    injected_env,
)
from svarog_harness.skills import Skill, scan_skills, skill_cards
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import Approval, RunState
from svarog_harness.tools.approval import RequestApprovalTool
from svarog_harness.tools.file_tools import file_tools
from svarog_harness.tools.memory_tools import RememberTool
from svarog_harness.tools.registry import ToolRegistry
from svarog_harness.tools.shell import BashTool
from svarog_harness.tools.skill_tools import ReadSkillTool
from svarog_harness.trace.lookup import (
    ApprovalNotFoundError,
    RunNotFoundError,
    RunNotResumableError,
)
from svarog_harness.trace.recorder import TraceRecorder
from svarog_harness.trace.viewer import (
    fetch_run,
    fetch_runs,
    render_run,
    render_runs_table,
)
from svarog_harness.verifier import Verifier, skill_checks

app = typer.Typer(
    name="svarog",
    help="Svarog — Git-native runtime for self-hosted AI agents.",
    no_args_is_help=True,
)
traces_app = typer.Typer(help="Просмотр traces выполненных runs.", no_args_is_help=True)
app.add_typer(traces_app, name="traces")
approvals_app = typer.Typer(help="Approval-запросы: список и решения.", no_args_is_help=True)
app.add_typer(approvals_app, name="approvals")
skills_app = typer.Typer(help="Скиллы: список и проверка.", no_args_is_help=True)
app.add_typer(skills_app, name="skills")
console = Console()


@app.callback()
def main() -> None:
    """Svarog CLI."""


@app.command()
def version() -> None:
    """Показать версию svarog-harness."""
    console.print(f"svarog-harness {__version__}")


@app.command()
def init(
    path: Annotated[
        Path | None, typer.Argument(help="Каталог agent-home (по умолчанию текущий)")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Перезаписать существующие файлы")] = False,
) -> None:
    """Создать agent-home: skills, memory (Flow A), policies, .gitignore (§8)."""
    target = (path or Path.cwd()).resolve()
    result = scaffold_agent_home(target, force=force)
    for created in result.created:
        console.print(f"[green]+[/green] {created.relative_to(target)}")
    for skipped in result.skipped:
        console.print(f"[dim]= {skipped.relative_to(target)} (существует, пропущено)[/dim]")

    async def init_memory_repo() -> None:
        repo = GitRepo(target / "memory")
        if not await repo.is_repo():
            await repo.init()
            await repo.ensure_identity()
            await repo.add_all()
            with contextlib.suppress(GitError):
                await repo.commit("svarog init: memory repo")

    asyncio.run(init_memory_repo())
    console.print(
        f"\n[bold]agent-home готов:[/bold] {target}\n"
        f'[dim]отредактируйте svarog.yaml (endpoint модели) и запустите `svarog run "…"`[/dim]'
    )


def _load_config_or_exit(project_dir: Path | None = None) -> SvarogConfig:
    try:
        return load_config(project_dir=project_dir)
    except ConfigError as exc:
        console.print(f"[red]ошибка конфигурации:[/red] {exc}")
        raise typer.Exit(code=1) from None


def _resolve_autonomy(
    cfg: SvarogConfig, *, yolo: bool, auto: bool, supervised: bool
) -> AutonomyMode:
    flags = {
        AutonomyMode.YOLO: yolo,
        AutonomyMode.AUTO: auto,
        AutonomyMode.SUPERVISED: supervised,
    }
    chosen = [mode for mode, enabled in flags.items() if enabled]
    if len(chosen) > 1:
        console.print("[red]флаги --yolo/--auto/--supervised взаимоисключающие[/red]")
        raise typer.Exit(code=1)
    return chosen[0] if chosen else cfg.runtime.autonomy


def _build_registry(
    workspace: Path,
    environment: ExecutionEnvironment,
    command_timeout_sec: float,
    skills: list[Skill],
    skill_load_sink: list[tuple[str, str | None]],
    *,
    memory_enabled: bool,
    memory_sink: list[dict[str, object]],
) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in file_tools(workspace):
        registry.register(tool)
    registry.register(BashTool(environment, command_timeout_sec))
    registry.register(RequestApprovalTool())
    if skills:
        registry.register(
            ReadSkillTool(
                skills, on_load=lambda name, version: skill_load_sink.append((name, version))
            )
        )
    if memory_enabled:
        registry.register(RememberTool(on_enqueue=lambda req: memory_sink.append(req.to_dict())))
    return registry


def _memory_dir(cfg: SvarogConfig) -> Path | None:
    """Каталог memory-репозитория (Flow A), если память включена в конфиге."""
    if cfg.memory.path is None:
        return None
    return cfg.memory.path.expanduser().resolve()


def _skills_dirs(cfg: SvarogConfig, workspace: Path) -> list[Path]:
    """Абсолютные пути каталогов skills из конфигурации."""
    dirs = []
    for raw in cfg.skills.paths:
        path = raw.expanduser()
        if not path.is_absolute():
            path = workspace / path
        dirs.append(path.resolve())
    return dirs


def _first_existing_skills_dir(cfg: SvarogConfig, workspace: Path) -> Path | None:
    """Первый существующий каталог skills — mount ro в sandbox (ADR-0002)."""
    for path in _skills_dirs(cfg, workspace):
        if path.is_dir():
            return path
    return None


async def _with_db[T](cfg: SvarogConfig, action: Callable[[AsyncSession], Awaitable[T]]) -> T:
    init_db(cfg.storage.db_path)
    engine = create_engine(cfg.storage.db_path.expanduser())
    try:
        factory = create_session_factory(engine)
        async with factory() as db:
            return await action(db)
    finally:
        await engine.dispose()


async def _recover_and_warn(recorder: TraceRecorder) -> None:
    """Recovery незавершенных runs при старте (ADR-0005)."""
    for run in await recorder.recover_interrupted_runs():
        console.print(
            f"[yellow]run {run.id[:8]} был прерван — переведен в suspended "
            f"(`svarog resume {run.id[:8]}`)[/yellow]"
        )


def _secret_store(cfg: SvarogConfig) -> SecretStore:
    return default_secret_store(cfg.secrets.path)


def _build_loop(
    cfg: SvarogConfig,
    workspace: Path,
    recorder: TraceRecorder,
    environment: ExecutionEnvironment,
    autonomy: AutonomyMode,
    store: SecretStore,
) -> AgentLoop:
    # Режим автономии и policy-правила фиксируются здесь, при старте run,
    # и не перечитываются во время исполнения (ADR-0010).
    policy = PolicyEngine(
        autonomy=autonomy,
        policies=cfg.policies,
        workspace=workspace,
        rules=load_policy_rules(workspace),
        skills_dirs=_skills_dirs(cfg, workspace),
    )
    scan = scan_skills(_skills_dirs(cfg, workspace))
    for skill_error in scan.errors:
        console.print(
            f"[yellow]skill пропущен ({skill_error.path.name}): {skill_error.reason}[/yellow]"
        )
    memory_dir = _memory_dir(cfg)
    memory_text = (
        read_memory(memory_dir, limit_bytes=cfg.memory.context_limit_bytes)
        if memory_dir is not None
        else ""
    )
    skill_load_sink: list[tuple[str, str | None]] = []
    memory_sink: list[dict[str, object]] = []
    return AgentLoop(
        default_provider(cfg.models, store),
        _build_registry(
            workspace,
            environment,
            cfg.sandbox.timeout_sec,
            scan.skills,
            skill_load_sink,
            memory_enabled=memory_dir is not None,
            memory_sink=memory_sink,
        ),
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
        on_text_delta=lambda delta: console.print(delta, end="", highlight=False),
        on_tool_call=lambda name, args: console.print(
            f"\n[dim]→ {name} {args}[/dim]", highlight=False
        ),
        on_notify=lambda name, reason: console.print(
            f"\n[bold yellow]⚡ notify:[/bold yellow] {name} — {reason}", highlight=False
        ),
    )


async def _run_task(
    cfg: SvarogConfig, workspace: Path, task: str, autonomy: AutonomyMode
) -> RunOutcome:
    # Flow C: подготовить workspace (pull + task branch) до старта sandbox,
    # чтобы контейнер видел рабочую ветку (ADR-0003).
    flow = WorkspaceFlow(GitRepo(workspace), cfg.git)
    prep = await flow.start(task)
    if prep.is_git and prep.branch:
        console.print(
            f"[dim]workspace: ветка {prep.branch}{' (после pull)' if prep.pulled else ''}[/dim]"
        )
    if prep.note:
        console.print(f"[yellow]{prep.note}[/yellow]")

    store = _secret_store(cfg)
    environment = create_environment(
        cfg.sandbox,
        workspace,
        skills_dir=_first_existing_skills_dir(cfg, workspace),
        env=injected_env(store, cfg.secrets.inject),  # только явно выданные секреты (§12)
    )
    await environment.start()
    try:

        async def action(db: AsyncSession) -> RunOutcome:
            recorder = TraceRecorder(db)
            await _recover_and_warn(recorder)
            loop = _build_loop(cfg, workspace, recorder, environment, autonomy, store)
            outcome = await loop.run(task, autonomy)
            await _drain_memory(cfg, db)
            await _verify(cfg, workspace, environment, recorder, outcome, store)
            await _autocommit_workspace(cfg, flow, prep, task, outcome)
            return outcome

        return await _with_db(cfg, action)
    finally:
        await environment.cleanup()


async def _verify(
    cfg: SvarogConfig,
    workspace: Path,
    environment: ExecutionEnvironment,
    recorder: TraceRecorder,
    outcome: RunOutcome,
    store: SecretStore,
) -> None:
    """Детерминированный verifier после completed-run (§6.11); пишет CheckResult."""
    if outcome.state is not RunState.COMPLETED:
        return
    run = await recorder.get_run(outcome.run_id)
    if run is None:
        return
    scan = scan_skills(_skills_dirs(cfg, workspace))
    loaded = await recorder.loaded_skill_names(run)
    checks = [*cfg.verifier.checks, *skill_checks(scan.skills, loaded)]
    if not checks and not cfg.verifier.secret_scan:
        return

    verifier = Verifier(environment, workspace)
    outcomes = await verifier.run(
        checks, secret_scan=cfg.verifier.secret_scan, known_values=store.values()
    )
    failed = [o for o in outcomes if not o.passed]
    for check in outcomes:
        await recorder.log_check_result(
            run, name=check.name, status=check.status, output=check.output
        )
        colour = "green" if check.passed else "red"
        console.print(f"[{colour}]check {check.status.value}[/{colour}] {check.name}")
    if failed:
        # Детерминированные проверки приоритетнее самооценки агента (§6.11).
        console.print(
            f"[red]verifier: {len(failed)} проверок не прошли — результат нельзя "
            f"считать корректным[/red]"
        )


async def _autocommit_workspace(
    cfg: SvarogConfig,
    flow: WorkspaceFlow,
    prep: WorkspacePrep,
    task: str,
    outcome: RunOutcome,
) -> None:
    """Flow C: закоммитить изменения workspace на task-ветке после run."""
    if not (prep.is_git and cfg.git.auto_commit and outcome.state is RunState.COMPLETED):
        return
    try:
        sha = await flow.commit_step(f"svarog: {task[:72]}", run_id=outcome.run_id)
    except SecretScanBlockedError as exc:
        console.print(f"[red]коммит workspace заблокирован secret scan:[/red]\n{exc}")
        return
    if sha is None:
        return
    branch = prep.branch or "HEAD"
    console.print(f"[dim]workspace закоммичен ({sha}) на {branch}[/dim]")
    if cfg.git.require_approval_for_push:
        console.print(f"[dim]push вручную: svarog push {branch}[/dim]")


async def _drain_memory(cfg: SvarogConfig, db: AsyncSession) -> None:
    """Применить очередь заявок памяти single writer'ом после run (ADR-0004)."""
    memory_dir = _memory_dir(cfg)
    if memory_dir is None or not memory_dir.is_dir():
        return
    writer = MemoryWriter(db, memory_dir)
    for row in await writer.drain():
        if row.error:
            console.print(f"[yellow]память: заявка отклонена — {row.error}[/yellow]")
        elif row.commit_sha:
            console.print(f"[dim]память обновлена ({row.commit_sha})[/dim]")


async def _resume_task(cfg: SvarogConfig, run_id: str) -> RunOutcome:
    async def action(db: AsyncSession) -> RunOutcome:
        recorder = TraceRecorder(db)
        await _recover_and_warn(recorder)
        run, raw_state = await recorder.load_resumable(run_id)
        state = LoopState.from_dict(raw_state)
        workspace = state.workspace
        if not workspace.is_dir():
            raise RunNotResumableError(f"workspace run'а больше не существует: {workspace}")
        # runtime/sandbox-настройки берутся из конфига проекта workspace,
        # режим автономии — заморожен в run (ADR-0010).
        run_cfg = load_config(project_dir=workspace)
        store = _secret_store(run_cfg)
        environment = create_environment(
            run_cfg.sandbox,
            workspace,
            skills_dir=_first_existing_skills_dir(run_cfg, workspace),
            env=injected_env(store, run_cfg.secrets.inject),
        )
        await environment.start()
        try:
            loop = _build_loop(
                run_cfg, workspace, recorder, environment, AutonomyMode(run.autonomy), store
            )
            outcome = await loop.resume(run, state)
            await _drain_memory(run_cfg, db)
            await _verify(run_cfg, workspace, environment, recorder, outcome, store)
            return outcome
        finally:
            await environment.cleanup()

    return await _with_db(cfg, action)


@app.command()
def run(
    task: Annotated[str, typer.Argument(help="Задача для агента")],
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Рабочая директория агента (по умолчанию cwd)"),
    ] = None,
    yolo: Annotated[bool, typer.Option("--yolo", help="Режим автономии yolo")] = False,
    auto: Annotated[bool, typer.Option("--auto", help="Режим автономии auto")] = False,
    supervised: Annotated[
        bool, typer.Option("--supervised", help="Режим автономии supervised")
    ] = False,
) -> None:
    """Выполнить задачу агентом в workspace (один agent run)."""
    workspace = (workspace or Path.cwd()).resolve()
    if not workspace.is_dir():
        console.print(f"[red]workspace не существует:[/red] {workspace}")
        raise typer.Exit(code=1)
    cfg = _load_config_or_exit(project_dir=workspace)
    autonomy = _resolve_autonomy(cfg, yolo=yolo, auto=auto, supervised=supervised)

    console.print(f"[bold]задача:[/bold] {task}")
    console.print(f"[dim]workspace: {workspace} | автономия: {autonomy.value}[/dim]\n")
    try:
        outcome = asyncio.run(_run_task(cfg, workspace, task, autonomy))
    except ApiKeyError as exc:
        console.print(f"[red]ошибка доступа к модели:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SandboxError as exc:
        console.print(f"[red]ошибка sandbox:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except PolicyRulesError as exc:
        console.print(f"[red]ошибка policy-правил:[/red] {exc}")
        raise typer.Exit(code=1) from None
    outcome = _interactive_approvals(cfg, outcome)
    _report_outcome(outcome, _failed_checks(cfg, outcome))


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="id приостановленного run'а или его префикс")],
) -> None:
    """Возобновить suspended run из checkpoint (ADR-0005)."""
    cfg = _load_config_or_exit()
    try:
        outcome = asyncio.run(_resume_task(cfg, run_id))
    except (RunNotFoundError, RunNotResumableError, ConfigError, PolicyRulesError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    except ApiKeyError as exc:
        console.print(f"[red]ошибка доступа к модели:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SandboxError as exc:
        console.print(f"[red]ошибка sandbox:[/red] {exc}")
        raise typer.Exit(code=1) from None
    outcome = _interactive_approvals(cfg, outcome)
    _report_outcome(outcome, _failed_checks(cfg, outcome))


def _failed_checks(cfg: SvarogConfig, outcome: RunOutcome) -> int:
    """Сколько verifier-проверок не прошло у completed-run (для exit-кода)."""
    if outcome.state is not RunState.COMPLETED:
        return 0

    async def action(db: AsyncSession) -> int:
        return await TraceRecorder(db).failed_check_count(outcome.run_id)

    return asyncio.run(_with_db(cfg, action))


def _show_approval(approval: Approval) -> None:
    """Показать фактическое действие (команду/аргументы), не пересказ (§12)."""
    payload = approval.payload or {}
    console.print(f"[bold]approval {approval.id[:8]}[/bold] | run {approval.run_id[:8]}")
    console.print(f"  действие: {approval.action_type}")
    if payload.get("tool"):
        console.print(f"  tool: {payload['tool']}")
    if payload.get("arguments"):
        console.print(
            f"  аргументы: {json.dumps(payload['arguments'], ensure_ascii=False, indent=2)}"
        )
    if payload.get("reason"):
        console.print(f"  причина: {payload['reason']}")


def _interactive_approvals(cfg: SvarogConfig, outcome: RunOutcome) -> RunOutcome:
    """Промпт решения прямо в терминале, затем resume — пока run не завершится.

    Асинхронный путь (ADR-0005) остается основным: без TTY команда выходит
    с кодом 3, решение принимается через `svarog approvals`, затем resume.
    """
    while outcome.state is RunState.WAITING_APPROVAL and sys.stdin.isatty():

        async def fetch(db: AsyncSession, run_id: str = outcome.run_id) -> list[Approval]:
            pending = await TraceRecorder(db).fetch_pending_approvals()
            return [a for a in pending if a.run_id == run_id]

        approvals = asyncio.run(_with_db(cfg, fetch))
        if not approvals:
            break
        console.print()
        for approval in approvals:
            _show_approval(approval)
            approved = typer.confirm("одобрить действие?", default=False)
            reason = None
            if not approved:
                reason = typer.prompt("причина отказа", default="", show_default=False) or None

            async def decide(
                db: AsyncSession,
                approval_id: str = approval.id,
                verdict: bool = approved,
                why: str | None = reason,
            ) -> None:
                recorder = TraceRecorder(db)
                found = await recorder.find_approval_by_prefix(approval_id)
                await recorder.decide_approval(
                    found, approved=verdict, decided_by="cli", reason=why
                )

            asyncio.run(_with_db(cfg, decide))
        outcome = asyncio.run(_resume_task(cfg, outcome.run_id))
    return outcome


def _report_outcome(outcome: RunOutcome, failed_checks: int = 0) -> None:
    console.print()
    stats = (
        f"run {outcome.run_id[:8]} | {outcome.iterations} итераций | "
        f"{outcome.tokens_used} токенов | ${outcome.cost_usd:.4f}"
    )
    if outcome.state is RunState.COMPLETED:
        if failed_checks:
            # Детерминированные checks приоритетнее самооценки агента (§6.11).
            console.print(f"[red]completed, но {failed_checks} проверок не прошли[/red] | {stats}")
            raise typer.Exit(code=4)
        console.print(f"[green]completed[/green] | {stats}")
    elif outcome.state is RunState.SUSPENDED:
        console.print(f"[yellow]suspended[/yellow] | {stats}")
        if outcome.error:
            console.print(f"[yellow]{outcome.error}[/yellow]")
        console.print(f"[dim]возобновить: svarog resume {outcome.run_id[:8]}[/dim]")
        raise typer.Exit(code=3)
    elif outcome.state is RunState.WAITING_APPROVAL:
        console.print(f"[magenta]waiting_approval[/magenta] | {stats}")
        if outcome.error:
            console.print(f"[magenta]{outcome.error}[/magenta]")
        console.print(
            f"[dim]решение: svarog approvals list → svarog approvals approve/deny <id>, "
            f"затем svarog resume {outcome.run_id[:8]}[/dim]"
        )
        raise typer.Exit(code=3)
    else:
        console.print(f"[red]{outcome.state.value}[/red] | {stats}")
        if outcome.error:
            console.print(f"[red]{outcome.error}[/red]")
        raise typer.Exit(code=2)


@traces_app.command("list")
def traces_list(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Сколько runs показать")] = 20,
) -> None:
    """Показать последние runs."""
    cfg = _load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        runs = await fetch_runs(db, limit=limit)
        if not runs:
            console.print('runs пока нет — запустите `svarog run "задача"`')
            return
        console.print(render_runs_table(runs))

    asyncio.run(_with_db(cfg, action))


@traces_app.command("show")
def traces_show(
    run_id: Annotated[str, typer.Argument(help="id run'а или его уникальный префикс")],
) -> None:
    """Показать полный trace одного run."""
    cfg = _load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        try:
            run, messages, tool_calls, checks = await fetch_run(db, run_id)
        except RunNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        console.print(render_run(run, messages, tool_calls, checks))

    asyncio.run(_with_db(cfg, action))


@approvals_app.command("list")
def approvals_list() -> None:
    """Показать ожидающие approval-запросы."""
    cfg = _load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        approvals = await TraceRecorder(db).fetch_pending_approvals()
        if not approvals:
            console.print("ожидающих approvals нет")
            return
        for approval in approvals:
            _show_approval(approval)
            console.print(f"  [dim]решение: svarog approvals approve/deny {approval.id[:8]}[/dim]")

    asyncio.run(_with_db(cfg, action))


def _decide_approval_command(approval_id: str, *, approved: bool, reason: str | None) -> None:
    cfg = _load_config_or_exit()

    async def action(db: AsyncSession) -> Approval:
        recorder = TraceRecorder(db)
        approval = await recorder.find_approval_by_prefix(approval_id)
        await recorder.decide_approval(approval, approved=approved, decided_by="cli", reason=reason)
        return approval

    try:
        approval = asyncio.run(_with_db(cfg, action))
    except ApprovalNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    verdict = "[green]одобрен[/green]" if approved else "[red]отклонен[/red]"
    console.print(f"approval {approval.id[:8]} {verdict}")
    console.print(f"[dim]продолжить run: svarog resume {approval.run_id[:8]}[/dim]")


@approvals_app.command("approve")
def approvals_approve(
    approval_id: Annotated[str, typer.Argument(help="id approval'а или его префикс")],
    reason: Annotated[str | None, typer.Option("--reason", help="Комментарий")] = None,
) -> None:
    """Одобрить действие; run возобновляется командой resume."""
    _decide_approval_command(approval_id, approved=True, reason=reason)


@approvals_app.command("deny")
def approvals_deny(
    approval_id: Annotated[str, typer.Argument(help="id approval'а или его префикс")],
    reason: Annotated[str | None, typer.Option("--reason", help="Причина отказа")] = None,
) -> None:
    """Отклонить действие; агент получит причину отказа при resume."""
    _decide_approval_command(approval_id, approved=False, reason=reason)


@skills_app.command("list")
def skills_list() -> None:
    """Показать доступные скиллы и их карточки."""
    cfg = _load_config_or_exit()
    workspace = Path.cwd().resolve()
    scan = scan_skills(_skills_dirs(cfg, workspace))
    if not scan.skills and not scan.errors:
        console.print("скиллов не найдено (проверьте skills.paths в svarog.yaml)")
        return
    for skill in scan.skills:
        console.print(skill.card())
    for skill_error in scan.errors:
        console.print(f"[yellow]пропущен ({skill_error.path.name}): {skill_error.reason}[/yellow]")


@skills_app.command("check")
def skills_check() -> None:
    """Проверить валидность SKILL.md всех скиллов; exit code 1 при ошибках."""
    cfg = _load_config_or_exit()
    workspace = Path.cwd().resolve()
    scan = scan_skills(_skills_dirs(cfg, workspace))
    for skill in scan.skills:
        console.print(f"[green]ok[/green] {skill.name} (v{skill.metadata.version})")
    for skill_error in scan.errors:
        console.print(f"[red]ошибка[/red] {skill_error.path}: {skill_error.reason}")
    if scan.errors:
        raise typer.Exit(code=1)
    console.print(f"проверено скиллов: {len(scan.skills)}, ошибок нет")


memory_app = typer.Typer(help="Память агента (Flow A).", no_args_is_help=True)
app.add_typer(memory_app, name="memory")


@memory_app.command("show")
def memory_show() -> None:
    """Показать память, как она попадёт в контекст."""
    cfg = _load_config_or_exit()
    memory_dir = _memory_dir(cfg)
    if memory_dir is None:
        console.print("память не настроена (задайте memory.path в svarog.yaml)")
        return
    text = read_memory(memory_dir, limit_bytes=cfg.memory.context_limit_bytes)
    console.print(text or "память пуста")


@memory_app.command("flush")
def memory_flush() -> None:
    """Применить очередь заявок памяти single writer'ом (обычно вызывается после run)."""
    cfg = _load_config_or_exit()
    memory_dir = _memory_dir(cfg)
    if memory_dir is None or not memory_dir.is_dir():
        console.print("память не настроена или каталог отсутствует")
        raise typer.Exit(code=1)

    async def action(db: AsyncSession) -> int:
        writer = MemoryWriter(db, memory_dir)
        rows = await writer.drain()
        for row in rows:
            if row.error:
                console.print(f"[yellow]отклонено: {row.error}[/yellow]")
            elif row.commit_sha:
                console.print(f"[green]{row.commit_sha}[/green] применено")
        return len(rows)

    count = asyncio.run(_with_db(cfg, action))
    console.print(f"обработано заявок: {count}")


@app.command()
def push(
    branch: Annotated[
        str | None, typer.Argument(help="Ветка для push (по умолчанию текущая)")
    ] = None,
    remote: Annotated[str, typer.Option("--remote", help="Remote")] = "origin",
) -> None:
    """Протолкнуть task-ветку в remote (Flow C). Protected ветки требуют approval."""
    cfg = _load_config_or_exit()
    workspace = Path.cwd().resolve()
    repo = GitRepo(workspace)
    flow = WorkspaceFlow(repo, cfg.git)

    async def do_push() -> str:
        if not await repo.is_repo():
            raise GitError("текущий каталог не является git-репозиторием")
        target = branch or await repo.current_branch()
        # Policy: push в protected ветку — critical-набор, approval в любом режиме (§3.6).
        policy = PolicyEngine(
            autonomy=cfg.runtime.autonomy, policies=cfg.policies, workspace=workspace
        )
        decision = policy.evaluate_action("git.push", {"branch": target})
        if decision.action is PolicyAction.REQUIRE_APPROVAL:
            raise GitError(
                f"push в '{target}' требует approval ({decision.reason}); "
                f"protected ветки нельзя пушить командой push напрямую"
            )
        findings = await flow.push_precheck(target)
        if findings:
            raise GitError(
                f"secret scan перед push нашёл {len(findings)} секрет(ов) — push отменён"
            )
        return await flow.push(target, remote=remote)

    try:
        result = asyncio.run(do_push())
    except GitError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]push выполнен[/green]\n{result}")


secrets_app = typer.Typer(
    help="SecretStore: имена секретов (значения не показываются).", no_args_is_help=True
)
app.add_typer(secrets_app, name="secrets")


@secrets_app.command("list")
def secrets_list() -> None:
    """Показать имена секретов из файла store (значения не раскрываются)."""
    cfg = _load_config_or_exit()
    names = default_secret_store(cfg.secrets.path).names()
    if not names:
        console.print("секретов в файле store нет (env-секреты по именам не перечисляются)")
        return
    for name in names:
        console.print(name)


@secrets_app.command("set")
def secrets_set(
    name: Annotated[str, typer.Argument(help="Имя секрета (например PROVIDER_API_KEY)")],
    value: Annotated[str, typer.Option("--value", prompt=True, hide_input=True, help="Значение")],
) -> None:
    """Записать секрет в файл store (права 0600). Значение вводится скрыто."""
    cfg = _load_config_or_exit()
    if cfg.secrets.path is None:
        console.print("[red]secrets.path не задан в конфигурации[/red]")
        raise typer.Exit(code=1)
    store = FileSecretStore(cfg.secrets.path.expanduser())
    store.set(name, value)
    console.print(f"[green]секрет '{name}' сохранён[/green] в {cfg.secrets.path}")
