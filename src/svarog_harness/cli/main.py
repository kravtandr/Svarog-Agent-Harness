"""Точка входа CLI `svarog` (§10.1).

Доступные команды: init, run, resume, chat, push, version;
traces list/show, approvals list/approve/deny, skills list/check,
memory show/flush, secrets list/set.
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
from svarog_harness.config.paths import memory_dir, skills_dirs
from svarog_harness.config.schema import AutonomyMode, SecretsConfig, SvarogConfig
from svarog_harness.gitflow import GitError, GitRepo, WorkspaceFlow, WorkspacePrep
from svarog_harness.llm.openai_compatible import ApiKeyError, auxiliary_provider
from svarog_harness.llm.provider import ChatMessage
from svarog_harness.mcp import MCPError, build_mcp_tools, connect_mcp_servers
from svarog_harness.memory import MemoryWriter, read_memory
from svarog_harness.policy import PolicyEngine, PolicyRulesError
from svarog_harness.policy.engine import PolicyAction
from svarog_harness.runtime.loop import RunOutcome
from svarog_harness.runtime.orchestrator import RunHooks, TaskRunner
from svarog_harness.sandbox import SandboxError
from svarog_harness.scaffold import (
    DEFAULT_API_KEY_REF,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    scaffold_agent_home,
)
from svarog_harness.secrets import (
    FileSecretStore,
    SecretStore,
    default_secret_store,
    selected_values,
)
from svarog_harness.skills import scan_skills
from svarog_harness.skills.curator import (
    CurationReport,
    CuratorStore,
    consolidate_layer2,
    prune_layer1,
    rewrite_description,
)
from svarog_harness.skills.models import Skill
from svarog_harness.skills.proposal import SkillProposalRequest
from svarog_harness.skills.proposal_manager import (
    SkillProposalManager,
    SkillProposalNotFoundError,
    SkillProposalStateError,
)
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.locks import default_lock_backend
from svarog_harness.storage.models import Approval, Run, RunState, SkillProposal
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
from svarog_harness.verifier import CheckOutcome

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


def _ensure_gitignored(target: Path) -> Path | None:
    """Добавить agent-home в .gitignore внешнего проекта, если он лежит внутри cwd.

    Данные агента (workspaces, artifacts, секреты) не должны попадать во внешний
    репозиторий проекта. Возвращает путь обновлённого .gitignore или None.
    """
    cwd = Path.cwd().resolve()
    try:
        rel = target.relative_to(cwd)
    except ValueError:
        return None  # agent-home вне проекта (например, ~/agent-home) — нечего игнорировать
    if rel == Path():
        return None  # agent-home совпадает с корнем проекта
    entry = rel.as_posix().rstrip("/") + "/"
    gitignore = cwd / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if entry in existing.splitlines():
        return gitignore
    block = existing
    if block and not block.endswith("\n"):
        block += "\n"
    block += f"\n# Svarog agent-home (данные агента, секреты) — не коммитить\n{entry}\n"
    gitignore.write_text(block, encoding="utf-8")
    return gitignore


@app.command()
def init(
    path: Annotated[
        Path | None, typer.Argument(help="Каталог agent-home (по умолчанию ./agent-home)")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Перезаписать существующие файлы")] = False,
    no_input: Annotated[
        bool,
        typer.Option("--no-input", "-y", help="Не задавать вопросов, взять значения по умолчанию"),
    ] = False,
    model: Annotated[str | None, typer.Option(help="Имя модели")] = None,
    base_url: Annotated[
        str | None, typer.Option("--base-url", help="Base URL OpenAI-совместимого endpoint")
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="API-ключ (сохраняется в secret store, не в svarog.yaml)"),
    ] = None,
) -> None:
    """Создать agent-home: skills, memory (Flow A), policies, .gitignore (§8).

    Без --no-input задаёт интерактивные вопросы (путь, модель, base_url, ключ).
    """
    interactive = not no_input and sys.stdin.isatty()

    if path is None and interactive:
        path = Path(typer.prompt("Каталог agent-home", default="./agent-home"))
    target = (path or Path.cwd() / "agent-home").expanduser().resolve()

    if interactive:
        model = model or typer.prompt("Модель", default=DEFAULT_MODEL)
        base_url = base_url or typer.prompt("Base URL endpoint", default=DEFAULT_BASE_URL)
        if api_key is None:
            api_key = (
                typer.prompt(
                    "API-ключ (Enter — пропустить; для локальной модели не нужен)",
                    default="",
                    hide_input=True,
                    show_default=False,
                )
                or None
            )
    model = model or DEFAULT_MODEL
    base_url = base_url or DEFAULT_BASE_URL
    api_key_ref = DEFAULT_API_KEY_REF if api_key else None

    result = scaffold_agent_home(
        target, force=force, model=model, base_url=base_url, api_key_ref=api_key_ref
    )
    for created in result.created:
        console.print(f"[green]+[/green] {created.relative_to(target)}")
    for skipped in result.skipped:
        console.print(f"[dim]= {skipped.relative_to(target)} (существует, пропущено)[/dim]")

    if api_key and api_key_ref:
        secrets_path = SecretsConfig().path
        assert secrets_path is not None  # дефолт схемы всегда задан
        store = FileSecretStore(secrets_path.expanduser())
        store.set(api_key_ref, api_key)
        console.print(f"[green]ключ сохранён[/green] в {secrets_path} (ref: {api_key_ref})")

    ignored = _ensure_gitignored(target)
    if ignored is not None:
        console.print(f"[dim]agent-home добавлен в {ignored}[/dim]")

    async def init_git_subrepo(path: Path, message: str) -> None:
        repo = GitRepo(path)
        if not await repo.is_repo():
            await repo.init()
            await repo.ensure_identity()
            await repo.add_all()
            with contextlib.suppress(GitError):
                await repo.commit(message)

    async def init_subrepos() -> None:
        # memory — Flow A (ADR-0004); skills — Flow B базовая ветка для proposals (§18).
        await init_git_subrepo(target / "memory", "svarog init: memory repo")
        await init_git_subrepo(target / "skills", "svarog init: skills repo")

    asyncio.run(init_subrepos())
    next_step = (
        'запустите `svarog run "…"`'
        if api_key or api_key_ref is None
        else f"добавьте ключ: `svarog secrets set {api_key_ref}`"
    )
    console.print(
        f"\n[bold]agent-home готов:[/bold] {target}\n"
        f"[dim]модель {model} @ {base_url}; {next_step}[/dim]"
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


async def _with_db[T](cfg: SvarogConfig, action: Callable[[AsyncSession], Awaitable[T]]) -> T:
    init_db(cfg.storage.db_path)
    engine = create_engine(cfg.storage.db_path.expanduser())
    try:
        factory = create_session_factory(engine)
        async with factory() as db:
            return await action(db)
    finally:
        await engine.dispose()


def _known_secret_values(cfg: SvarogConfig, store: SecretStore) -> frozenset[str]:
    refs = list(cfg.secrets.inject)
    refs.extend(
        provider.api_key_ref
        for provider in cfg.models.providers.values()
        if provider.api_key_ref is not None
    )
    for server in cfg.mcp.servers.values():
        refs.extend(server.env_refs)
    if cfg.gateway.token_ref is not None:
        refs.append(cfg.gateway.token_ref)
    if cfg.telegram.token_ref is not None:
        refs.append(cfg.telegram.token_ref)
    return store.values() | selected_values(store, refs)


def _is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


def _console_hooks() -> RunHooks:
    """RunHooks, печатающие ход прогона задачи в терминал (§21)."""

    def on_prep(prep: WorkspacePrep) -> None:
        if prep.is_git and prep.branch:
            suffix = " (после pull)" if prep.pulled else ""
            console.print(f"[dim]workspace: ветка {prep.branch}{suffix}[/dim]")
        if prep.note:
            console.print(f"[yellow]{prep.note}[/yellow]")

    def on_recovered(run: Run) -> None:
        console.print(
            f"[yellow]run {run.id[:8]} был прерван — переведен в suspended "
            f"(`svarog resume {run.id[:8]}`)[/yellow]"
        )

    def on_check(check: CheckOutcome) -> None:
        colour = "green" if check.passed else "red"
        console.print(f"[{colour}]check {check.status.value}[/{colour}] {check.name}")

    def on_commit(sha: str, branch: str, needs_push: bool) -> None:
        console.print(f"[dim]workspace закоммичен ({sha}) на {branch}[/dim]")
        if needs_push:
            console.print(f"[dim]push вручную: svarog push {branch}[/dim]")

    def on_memory(commit_sha: str | None, error: str | None) -> None:
        if error:
            console.print(f"[yellow]память: заявка отклонена — {error}[/yellow]")
        elif commit_sha:
            console.print(f"[dim]память обновлена ({commit_sha})[/dim]")

    def on_proposal(proposal: SkillProposal) -> None:
        if proposal.status.value == "pending":
            console.print(
                f"[cyan]skill proposal[/cyan] {proposal.skill_name} → ветка {proposal.branch} "
                f"[dim](review: svarog skills proposals show {proposal.id[:8]})[/dim]"
            )
        else:
            console.print(
                f"[yellow]skill proposal {proposal.skill_name}: {proposal.status.value}[/yellow]"
            )
            for message in SkillProposalManager.validation_messages(proposal):
                console.print(f"[yellow]  - {message}[/yellow]")

    return RunHooks(
        on_skill_skipped=lambda name, reason: console.print(
            f"[yellow]skill пропущен ({name}): {reason}[/yellow]"
        ),
        on_workspace_prep=on_prep,
        on_recovered=on_recovered,
        on_text_delta=lambda delta: console.print(delta, end="", highlight=False),
        on_tool_call=lambda name, args: console.print(
            f"\n[dim]→ {name} {args}[/dim]", highlight=False
        ),
        on_notify=lambda name, reason: console.print(
            f"\n[bold yellow]⚡ notify:[/bold yellow] {name} — {reason}", highlight=False
        ),
        on_check=on_check,
        on_verify_failed=lambda count: console.print(
            f"[red]verifier: {count} проверок не прошли — результат нельзя считать корректным[/red]"
        ),
        on_commit=on_commit,
        on_commit_blocked=lambda msg: console.print(
            f"[red]коммит workspace заблокирован secret scan:[/red]\n{msg}"
        ),
        on_memory=on_memory,
        on_proposal=on_proposal,
    )


async def _run_task(
    cfg: SvarogConfig, workspace: Path, task: str, autonomy: AutonomyMode
) -> RunOutcome:
    return await TaskRunner(cfg, workspace).run_once(task, autonomy, hooks=_console_hooks())


async def _resume_task(cfg: SvarogConfig, run_id: str) -> RunOutcome:
    return await TaskRunner(cfg, Path.cwd().resolve()).resume(run_id, hooks=_console_hooks())


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


_CHAT_HISTORY_LIMIT = 24  # сообщений диалога в контексте, чтобы не раздувать промпт


@app.command()
def chat(
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
    """Интерактивная сессия: каждое сообщение — run в общей session (§10.1)."""
    workspace = (workspace or Path.cwd()).resolve()
    if not workspace.is_dir():
        console.print(f"[red]workspace не существует:[/red] {workspace}")
        raise typer.Exit(code=1)
    cfg = _load_config_or_exit(project_dir=workspace)
    autonomy = _resolve_autonomy(cfg, yolo=yolo, auto=auto, supervised=supervised)
    console.print(
        f"[bold]svarog chat[/bold] | workspace: {workspace} | автономия: {autonomy.value}\n"
        f"[dim]пустая строка или /quit — выход[/dim]"
    )
    try:
        asyncio.run(_chat_session(cfg, workspace, autonomy))
    except ApiKeyError as exc:
        console.print(f"[red]ошибка доступа к модели:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SandboxError as exc:
        console.print(f"[red]ошибка sandbox:[/red] {exc}")
        raise typer.Exit(code=1) from None


def _read_user_line(prompt: str) -> str:
    """Прочитать строку ввода, декодируя её целиком в UTF-8.

    `console.input`/`input()` в рабочем потоке (asyncio.to_thread) читают stdin
    без readline и по чанкам — на границе буфера многобайтовый символ (кириллица)
    рвётся, что даёт UnicodeDecodeError. Читаем сырые байты строки из буфера stdin
    и декодируем разом (errors="replace" на всякий случай). EOF → EOFError.
    """
    console.print(prompt, end="")
    console.file.flush()
    raw = sys.stdin.buffer.readline()
    if not raw:
        raise EOFError
    return raw.decode("utf-8", errors="replace")


async def _chat_session(cfg: SvarogConfig, workspace: Path, autonomy: AutonomyMode) -> None:
    runner = TaskRunner(cfg, workspace)
    hooks = _console_hooks()
    backends = await connect_mcp_servers(cfg.mcp, runner.store)
    mcp_tools = build_mcp_tools(backends)
    environment = runner.build_environment()
    await environment.start()
    try:

        async def action(db: AsyncSession) -> None:
            recorder = TraceRecorder(db)
            await runner.recover(recorder, hooks)
            session_id: str | None = None
            history: list[ChatMessage] = []
            while True:
                try:
                    task = (
                        await asyncio.to_thread(_read_user_line, "\n[bold cyan]› [/bold cyan]")
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not task or task in {"/quit", "/exit"}:
                    break
                proposal_sink: list[SkillProposalRequest] = []
                excluded = frozenset(await CuratorStore(db).archived_names())
                loop = runner.build_loop(
                    recorder,
                    environment,
                    autonomy,
                    hooks,
                    proposal_sink,
                    excluded_skills=excluded,
                    mcp_tools=mcp_tools,
                )
                outcome = await loop.run(task, autonomy, session_id=session_id, history=history)
                await runner.drain_memory(db, hooks)
                await runner.drain_proposals(db, proposal_sink, outcome.run_id, hooks)
                if session_id is None:
                    run = await recorder.get_run(outcome.run_id)
                    session_id = run.session_id if run else None
                history.append(ChatMessage(role="user", content=task))
                history.append(
                    ChatMessage(role="assistant", content=outcome.final_answer or "(без ответа)")
                )
                history[:] = history[-_CHAT_HISTORY_LIMIT:]
                _print_chat_turn(outcome)

        await runner.with_db(action)
    finally:
        await environment.cleanup()
        for backend in backends:
            with contextlib.suppress(Exception):
                await backend.close()


def _print_chat_turn(outcome: RunOutcome) -> None:
    stats = f"{outcome.iterations} итер. | ${outcome.cost_usd:.4f}"
    if outcome.state is RunState.COMPLETED:
        console.print(f"[dim]— {stats}[/dim]")
    elif outcome.state is RunState.WAITING_APPROVAL:
        console.print(
            f"[magenta]ожидает approval[/magenta] | {stats} "
            f"[dim](svarog approvals list, затем resume {outcome.run_id[:8]})[/dim]"
        )
    else:
        label = outcome.state.value
        console.print(f"[yellow]{label}[/yellow] | {stats}")
        if outcome.error:
            console.print(f"[yellow]{outcome.error}[/yellow]")


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
            if approval.action_type == "user.question":
                _answer_question_interactive(cfg, approval)
                continue
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


def _answer_question_interactive(cfg: SvarogConfig, approval: Approval) -> None:
    """ask_user: показать вопрос и записать текстовый ответ (§6.5)."""
    payload = approval.payload or {}
    console.print(f"[bold]вопрос {approval.id[:8]}[/bold] | run {approval.run_id[:8]}")
    console.print(f"  [cyan]{payload.get('question') or payload.get('reason') or ''}[/cyan]")
    answer = typer.prompt(
        "ваш ответ (Enter — продолжить без ответа)", default="", show_default=False
    )

    async def record(db: AsyncSession, approval_id: str = approval.id, text: str = answer) -> None:
        recorder = TraceRecorder(db)
        found = await recorder.find_approval_by_prefix(approval_id)
        await recorder.answer_question(found, answer=text, answered_by="cli")

    asyncio.run(_with_db(cfg, record))


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
        if outcome.error and outcome.error.startswith("verifier:"):
            raise typer.Exit(code=4)
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


@approvals_app.command("answer")
def approvals_answer(
    approval_id: Annotated[str, typer.Argument(help="id вопроса ask_user или его префикс")],
    text: Annotated[str, typer.Argument(help="Текст ответа; пусто — продолжить без ответа")] = "",
) -> None:
    """Ответить на вопрос ask_user; run возобновляется командой resume (§6.5)."""
    cfg = _load_config_or_exit()

    async def action(db: AsyncSession) -> Approval:
        recorder = TraceRecorder(db)
        approval = await recorder.find_approval_by_prefix(approval_id)
        await recorder.answer_question(approval, answer=text, answered_by="cli")
        return approval

    try:
        approval = asyncio.run(_with_db(cfg, action))
    except ApprovalNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"вопрос {approval.id[:8]} [green]отвечен[/green]")
    console.print(f"[dim]продолжить run: svarog resume {approval.run_id[:8]}[/dim]")


@skills_app.command("list")
def skills_list() -> None:
    """Показать доступные скиллы и их карточки."""
    cfg = _load_config_or_exit()
    workspace = Path.cwd().resolve()
    scan = scan_skills(skills_dirs(cfg, workspace))
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
    scan = scan_skills(skills_dirs(cfg, workspace))
    for skill in scan.skills:
        console.print(f"[green]ok[/green] {skill.name} (v{skill.metadata.version})")
    for skill_error in scan.errors:
        console.print(f"[red]ошибка[/red] {skill_error.path}: {skill_error.reason}")
    if scan.errors:
        raise typer.Exit(code=1)
    console.print(f"проверено скиллов: {len(scan.skills)}, ошибок нет")


proposals_app = typer.Typer(
    help="Skill proposals (Flow B): review, merge, reject.", no_args_is_help=True
)
skills_app.add_typer(proposals_app, name="proposals")


def _proposals_skills_dir(cfg: SvarogConfig) -> Path:
    dirs = skills_dirs(cfg, Path.cwd().resolve())
    if not dirs:
        console.print("[red]skills.paths пуст в svarog.yaml[/red]")
        raise typer.Exit(code=1)
    return dirs[0]


@proposals_app.command("list")
def skills_proposals_list() -> None:
    """Показать skill proposals, ожидающие review (Flow B, §18)."""
    cfg = _load_config_or_exit()
    skills_dir = _proposals_skills_dir(cfg)

    async def action(db: AsyncSession) -> None:
        proposals = await SkillProposalManager(db, skills_dir).list_pending()
        if not proposals:
            console.print("ожидающих skill proposals нет")
            return
        for proposal in proposals:
            console.print(
                f"[cyan]{proposal.id[:8]}[/cyan] {proposal.skill_name} "
                f"({proposal.action}) → ветка {proposal.branch}"
            )
            console.print(
                f"  [dim]review: svarog skills proposals show {proposal.id[:8]} → "
                f"approve/reject {proposal.id[:8]}[/dim]"
            )

    asyncio.run(_with_db(cfg, action))


@proposals_app.command("show")
def skills_proposals_show(
    proposal_id: Annotated[str, typer.Argument(help="id proposal'а или его префикс")],
) -> None:
    """Показать diff и метаданные skill proposal (фактические изменения, §12)."""
    cfg = _load_config_or_exit()
    skills_dir = _proposals_skills_dir(cfg)

    async def action(db: AsyncSession) -> None:
        try:
            proposal = await SkillProposalManager(db, skills_dir).get(proposal_id)
        except SkillProposalNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        console.print(f"[bold]proposal {proposal.id[:8]}[/bold] | {proposal.status.value}")
        console.print(f"  скилл: {proposal.skill_name} ({proposal.action})")
        console.print(f"  ветка: {proposal.branch} → {proposal.base}")
        if proposal.note:
            console.print(f"  примечание: {proposal.note}")
        for message in SkillProposalManager.validation_messages(proposal):
            console.print(f"  [yellow]валидация: {message}[/yellow]")
        console.print(proposal.diff or "(diff пуст)")

    asyncio.run(_with_db(cfg, action))


def _decide_proposal(proposal_id: str, *, approved: bool, reason: str | None) -> None:
    cfg = _load_config_or_exit()
    skills_dir = _proposals_skills_dir(cfg)

    async def action(db: AsyncSession) -> tuple[SkillProposal, str | None]:
        manager = SkillProposalManager(db, skills_dir)
        proposal = await manager.get(proposal_id)
        sha = await manager.decide(proposal, approved=approved, decided_by="cli", reason=reason)
        return proposal, sha

    try:
        proposal, sha = asyncio.run(_with_db(cfg, action))
    except SkillProposalNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    except (SkillProposalStateError, GitError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    if approved:
        console.print(f"[green]proposal {proposal.id[:8]} влит[/green] в {proposal.base} ({sha})")
    else:
        console.print(f"[yellow]proposal {proposal.id[:8]} отклонён[/yellow]")


@proposals_app.command("approve")
def skills_proposals_approve(
    proposal_id: Annotated[str, typer.Argument(help="id proposal'а или его префикс")],
    reason: Annotated[str | None, typer.Option("--reason", help="Комментарий")] = None,
) -> None:
    """Одобрить и влить skill proposal в базовую ветку (§18)."""
    _decide_proposal(proposal_id, approved=True, reason=reason)


@proposals_app.command("reject")
def skills_proposals_reject(
    proposal_id: Annotated[str, typer.Argument(help="id proposal'а или его префикс")],
    reason: Annotated[str | None, typer.Option("--reason", help="Причина отказа")] = None,
) -> None:
    """Отклонить skill proposal и удалить его ветку."""
    _decide_proposal(proposal_id, approved=False, reason=reason)


@skills_app.command("curate")
def skills_curate(
    semantic: Annotated[
        bool,
        typer.Option("--semantic", help="Слой 2: LLM-консолидация на auxiliary-модели (opt-in)"),
    ] = False,
) -> None:
    """Curator: слой 1 (lifecycle по usage) и опц. слой 2 (LLM-консолидация, §18.1)."""
    cfg = _load_config_or_exit()
    workspace = Path.cwd().resolve()
    scan = scan_skills(skills_dirs(cfg, workspace))

    async def action(db: AsyncSession) -> None:
        transitions = await prune_layer1(db, scan.skills, cfg.curator)
        if transitions:
            for t in transitions:
                console.print(
                    f"[cyan]{t.skill_name}[/cyan]: {t.old.value} → {t.new.value} "
                    f"[dim]({t.reason})[/dim]"
                )
        else:
            console.print("curator слой 1: lifecycle-изменений нет")
        if semantic or cfg.curator.semantic:
            await _curate_semantic(cfg, workspace, scan.skills, db)

    asyncio.run(_with_db(cfg, action))


async def _curate_semantic(
    cfg: SvarogConfig, workspace: Path, skills: list[Skill], db: AsyncSession
) -> None:
    """Слой 2: LLM-находки → отчёт в artifacts/ + description-proposals (§18.1)."""
    try:
        provider = auxiliary_provider(cfg.models, default_secret_store(cfg.secrets.path))
    except ApiKeyError as exc:
        console.print(f"[red]слой 2 недоступен: {exc}[/red]")
        return
    console.print("[dim]curator слой 2: анализ библиотеки auxiliary-моделью…[/dim]")
    report = await consolidate_layer2(provider, skills)
    path = _write_curation_report(workspace, report)
    console.print(f"[dim]отчёт: {path}[/dim]")
    if report.parse_error:
        console.print(
            f"[yellow]слой 2: LLM вернул неразбираемый ответ ({report.parse_error})[/yellow]"
        )
        return
    if not report.findings:
        console.print("curator слой 2: находок нет")
        return
    for finding in report.findings:
        console.print(
            f"[magenta]{finding.kind}[/magenta] {', '.join(finding.skills)}: {finding.detail}"
        )
    await _propose_description_improvements(cfg, workspace, skills, report, db)


def _write_curation_report(workspace: Path, report: CurationReport) -> Path:
    from datetime import UTC, datetime

    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = artifacts / f"skill-curation-{stamp}.md"
    path.write_text(report.to_markdown(), encoding="utf-8")
    return path


async def _propose_description_improvements(
    cfg: SvarogConfig,
    workspace: Path,
    skills: list[Skill],
    report: CurationReport,
    db: AsyncSession,
) -> None:
    """Содержательные правки — только через proposals (Flow B, §18.1)."""
    by_name = {s.name: s for s in skills}
    skills_dir = skills_dirs(cfg, workspace)[0] if cfg.skills.paths else None
    if skills_dir is None:
        return
    manager = SkillProposalManager(db, skills_dir)
    store = default_secret_store(cfg.secrets.path)
    for finding in report.improvements():
        for name in finding.skills:
            skill = by_name.get(name)
            # Curator предлагает правки только agent-created скиллов (§18.1).
            if skill is None or skill.metadata.provenance != "agent":
                continue
            files = {"SKILL.md": rewrite_description(skill, finding.suggested_description or "")}
            request = SkillProposalRequest(
                skill_name=name,
                action="update",
                files=files,
                note=f"curator: улучшить описание — {finding.detail}",
            )
            proposal = await manager.persist(request, known_values=_known_secret_values(cfg, store))
            console.print(
                f"[cyan]proposal[/cyan] {name}: обновить описание "
                f"[dim]({proposal.status.value}, {proposal.id[:8]})[/dim]"
            )


def _set_pin(name: str, pinned: bool) -> None:
    cfg = _load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        await CuratorStore(db).set_pinned(name, pinned)

    asyncio.run(_with_db(cfg, action))
    verb = "закреплён" if pinned else "откреплён"
    console.print(f"скилл '{name}' {verb} (pinned={str(pinned).lower()})")


@skills_app.command("pin")
def skills_pin(
    name: Annotated[str, typer.Argument(help="Имя скилла")],
) -> None:
    """Закрепить скилл: вывести из-под автоматических lifecycle-переходов (§18.1)."""
    _set_pin(name, True)


@skills_app.command("unpin")
def skills_unpin(
    name: Annotated[str, typer.Argument(help="Имя скилла")],
) -> None:
    """Снять закрепление скилла."""
    _set_pin(name, False)


memory_app = typer.Typer(help="Память агента (Flow A).", no_args_is_help=True)
app.add_typer(memory_app, name="memory")


@memory_app.command("show")
def memory_show() -> None:
    """Показать память, как она попадёт в контекст."""
    cfg = _load_config_or_exit()
    mem_dir = memory_dir(cfg)
    if mem_dir is None:
        console.print("память не настроена (задайте memory.path в svarog.yaml)")
        return
    text = read_memory(mem_dir, limit_bytes=cfg.memory.context_limit_bytes)
    console.print(text or "память пуста")


@memory_app.command("flush")
def memory_flush() -> None:
    """Применить очередь заявок памяти single writer'ом (обычно вызывается после run)."""
    cfg = _load_config_or_exit()
    mem_dir = memory_dir(cfg)
    if mem_dir is None or not mem_dir.is_dir():
        console.print("память не настроена или каталог отсутствует")
        raise typer.Exit(code=1)

    store = default_secret_store(cfg.secrets.path)

    async def action(db: AsyncSession) -> int:
        writer = MemoryWriter(db, mem_dir, lock=default_lock_backend(cfg.storage.db_path))
        rows = await writer.drain(known_values=_known_secret_values(cfg, store))
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
    store = default_secret_store(cfg.secrets.path)

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
        findings = await flow.push_precheck(target, known_values=_known_secret_values(cfg, store))
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


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="Адрес прослушивания")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Порт")] = 8080,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Рабочая директория агента (по умолчанию cwd)"),
    ] = None,
) -> None:
    """Запустить REST/WebSocket API gateway (§10.4). Нужен extra `server`."""
    workspace = (workspace or Path.cwd()).resolve()
    if not workspace.is_dir():
        console.print(f"[red]workspace не существует:[/red] {workspace}")
        raise typer.Exit(code=1)
    cfg = _load_config_or_exit(project_dir=workspace)
    token_ref = cfg.gateway.token_ref
    token: str | None = None
    if token_ref is None and not _is_loopback_host(host):
        console.print(
            "[red]gateway.token_ref обязателен для сетевого bind[/red] "
            "(используйте 127.0.0.1 или настройте bearer-token в SecretStore)"
        )
        raise typer.Exit(code=1)
    if token_ref is not None:
        token = default_secret_store(cfg.secrets.path).get(token_ref)
        if not token:
            console.print(
                f"[red]секрет '{token_ref}' не найден[/red] в SecretStore/окружении; "
                f"svarog secrets set {token_ref}"
            )
            raise typer.Exit(code=1)
    try:
        import uvicorn

        from svarog_harness.gateway import GatewayService
        from svarog_harness.gateway.api import create_app
    except ImportError:
        console.print(
            "[red]gateway требует опциональные зависимости:[/red] "
            "uv pip install 'svarog-harness[server]'"
        )
        raise typer.Exit(code=1) from None
    api = create_app(GatewayService(cfg, workspace), bearer_token=token)
    console.print(
        f"[green]Svarog gateway[/green] http://{host}:{port} | workspace: {workspace}\n"
        f"[dim]POST /runs · GET /runs/{{id}} · WS /runs/{{id}}/events · "
        f"GET /approvals · POST /approvals/{{id}}[/dim]"
    )
    uvicorn.run(api, host=host, port=port, log_level="info")


@app.command()
def telegram(
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Рабочая директория агента (по умолчанию cwd)"),
    ] = None,
) -> None:
    """Запустить Telegram-бота (§10.2). Токен — секрет (telegram.token_ref)."""
    workspace = (workspace or Path.cwd()).resolve()
    if not workspace.is_dir():
        console.print(f"[red]workspace не существует:[/red] {workspace}")
        raise typer.Exit(code=1)
    cfg = _load_config_or_exit(project_dir=workspace)
    tg = cfg.telegram
    if tg.token_ref is None:
        console.print("[red]telegram.token_ref не задан[/red] (имя секрета с bot-токеном)")
        raise typer.Exit(code=1)
    if not tg.allowed_users:
        console.print(
            "[red]telegram.allowed_users пуст[/red] — задайте allowlist user-id "
            "(бот без allowlist отвечает всем отказом)"
        )
        raise typer.Exit(code=1)
    token = default_secret_store(cfg.secrets.path).get(tg.token_ref)
    if not token:
        console.print(
            f"[red]секрет '{tg.token_ref}' не найден[/red] в SecretStore/окружении; "
            f"svarog secrets set {tg.token_ref}"
        )
        raise typer.Exit(code=1)

    from svarog_harness.gateway import GatewayService
    from svarog_harness.gateway.telegram import (
        HttpxTelegramTransport,
        TelegramBot,
    )

    bot = TelegramBot(
        GatewayService(cfg, workspace),
        HttpxTelegramTransport(token),
        allowed_users=set(tg.allowed_users),
        poll_timeout=tg.poll_timeout_sec,
    )
    console.print(
        f"[green]Svarog Telegram bot[/green] | workspace: {workspace} | "
        f"allowlist: {len(tg.allowed_users)} user(s)\n[dim]Ctrl-C для остановки[/dim]"
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(bot.run_forever())


mcp_app = typer.Typer(help="MCP-серверы: discovery инструментов (§9).", no_args_is_help=True)
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("list")
def mcp_list() -> None:
    """Подключить настроенные MCP-серверы и показать обнаруженные инструменты."""
    cfg = _load_config_or_exit()
    if not cfg.mcp.servers:
        console.print("MCP-серверы не настроены (секция mcp.servers в svarog.yaml)")
        return
    store = default_secret_store(cfg.secrets.path)

    async def discover() -> None:
        backends = await connect_mcp_servers(cfg.mcp, store)
        try:
            tools = build_mcp_tools(backends)
            if not tools:
                console.print("инструменты на MCP-серверах не обнаружены")
                return
            for tool in tools:
                console.print(
                    f"[cyan]{tool.name}[/cyan] [dim](risk={tool.risk_level.value}, "
                    f"action={tool.action_type}, по умолчанию approval)[/dim]"
                )
                if tool.description:
                    console.print(f"  {tool.description}")
        finally:
            for backend in backends:
                with contextlib.suppress(Exception):
                    await backend.close()

    try:
        asyncio.run(discover())
    except MCPError as exc:
        console.print(f"[red]MCP: {exc}[/red]")
        raise typer.Exit(code=1) from None


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
