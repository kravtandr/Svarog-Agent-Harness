"""Точка входа CLI `svarog` (§10.1).

Доступные команды: init, install, run, resume, chat, push, rewind, doctor, version;
traces list/show, sessions list/rename, approvals list/approve/deny,
skills list/check, memory show/flush, secrets list/set.
"""

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from prompt_toolkit.shortcuts import prompt as prompt_toolkit
from rich.table import Table
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness import __version__
from svarog_harness.cli import install as install_module
from svarog_harness.cli import remote as remote_module
from svarog_harness.cli._shared import console
from svarog_harness.cli._shared import load_config_or_exit as _load_config_or_exit
from svarog_harness.cli._shared import resolve_autonomy as _resolve_autonomy
from svarog_harness.cli.chat_display import format_tool_call
from svarog_harness.cli.chat_engine import (
    ChatEngine,
    record_gate_answer,
    record_gate_decision,
)
from svarog_harness.cli.chat_engine import (
    with_db as _with_db,
)
from svarog_harness.cli.chat_settings import patch_project_config
from svarog_harness.cli.init_executor import (
    ClaudeAnswers,
    ExecutorSetupError,
    OpencodeAnswers,
    executor_setup_yaml_patch,
    resolve_executor_setup,
)
from svarog_harness.cli.init_images import (
    ExecutorAdapter,
    ExecutorImageBuildError,
    build_executor_image,
)
from svarog_harness.cli.policies import policies_app
from svarog_harness.config.loader import ConfigError, load_config
from svarog_harness.config.paths import (
    WorkspaceLayoutError,
    memory_dir,
    skills_dirs,
    workspace_layout_violations,
)
from svarog_harness.config.schema import AutonomyMode, SecretsConfig, SvarogConfig
from svarog_harness.gitflow import (
    GitError,
    GitRepo,
    WorkspaceFlow,
    WorkspacePrep,
    separate_gitdir_for,
)
from svarog_harness.llm.openai_compatible import ApiKeyError, auxiliary_provider
from svarog_harness.mcp import MCPError, build_mcp_tools, connect_mcp_servers
from svarog_harness.memory import MemoryWriter, read_memory
from svarog_harness.memory.curator import MemoryAuditReport, audit_memory
from svarog_harness.memory.dream import build_dream_task
from svarog_harness.memory.proposal_manager import (
    MemoryProposalManager,
    MemoryProposalNotFoundError,
    MemoryProposalStateError,
)
from svarog_harness.policy import PolicyEngine, PolicyRulesError
from svarog_harness.policy.engine import PolicyAction
from svarog_harness.runtime.config_snapshot import config_digest
from svarog_harness.runtime.loop import RunOutcome
from svarog_harness.runtime.orchestrator import (
    ConfigDriftError,
    RunHooks,
    RunProfile,
    TaskRunner,
)
from svarog_harness.sandbox import SandboxError
from svarog_harness.scaffold import (
    DEFAULT_API_KEY_REF,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    scaffold_agent_home,
)
from svarog_harness.scheduler.schedule import ScheduleSpecError, next_run_after, parse_spec
from svarog_harness.scheduler.store import JobNotFoundError, JobStore, ProtectedJobError
from svarog_harness.scheduler.system_jobs import DREAM_JOB_NAME, ensure_system_jobs
from svarog_harness.scheduler.ticker import JobRunRequest, tick
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
from svarog_harness.storage.locks import default_lock_backend
from svarog_harness.storage.models import (
    Approval,
    CronJob,
    JobOrigin,
    MemoryProposal,
    Run,
    RunState,
    ScheduleKind,
    SkillProposal,
    utcnow,
)
from svarog_harness.trace.lookup import (
    ApprovalNotFoundError,
    RunNotFoundError,
    RunNotResumableError,
    SessionNotFoundError,
    find_run_by_prefix,
    find_session_by_prefix,
)
from svarog_harness.trace.recorder import TraceRecorder, WorkspaceBusyError
from svarog_harness.trace.viewer import (
    fetch_run,
    fetch_runs,
    fetch_sessions,
    render_run,
    render_runs_table,
    render_sessions_table,
    run_detail_to_dict,
    run_to_dict,
    session_to_dict,
)
from svarog_harness.verifier import CheckOutcome

app = typer.Typer(
    name="svarog",
    help="Svarog — Git-native runtime for self-hosted AI agents.",
    no_args_is_help=True,
)
traces_app = typer.Typer(help="Просмотр traces выполненных runs.", no_args_is_help=True)
app.add_typer(traces_app, name="traces")
sessions_app = typer.Typer(
    help="Сессии: список, поиск, переименование (продолжение — chat --session).",
    no_args_is_help=True,
)
app.add_typer(sessions_app, name="sessions")
approvals_app = typer.Typer(help="Approval-запросы: список и решения.", no_args_is_help=True)
app.add_typer(approvals_app, name="approvals")
skills_app = typer.Typer(help="Скиллы: список и проверка.", no_args_is_help=True)
app.add_typer(skills_app, name="skills")
app.add_typer(policies_app, name="policies")
# Thin CLI cloud-режима (ADR-0017 §3): svarog remote … / svarog login.
app.add_typer(remote_module.remote_app, name="remote")
app.command("login")(remote_module.login)
# svarog install: env + alias в shell rc и symlink на ~/.svarog/svarog.yaml.
app.command("install")(install_module.install)


@app.callback()
def main() -> None:
    """Svarog CLI."""


@app.command()
def version() -> None:
    """Показать версию svarog-harness."""
    console.print(f"svarog-harness {__version__}")


_DOCTOR_STYLE = {"ok": "green", "warn": "yellow", "fail": "red"}


def _prompt_secret(text: str) -> str:
    """Запросить секрет с маской ``*``, не выводя его в scrollback."""
    return prompt_toolkit(f"{text}: ", is_password=True)


@app.command()
def doctor(
    json_output: Annotated[
        bool, typer.Option("--json", help="Машиночитаемый вывод (JSON-массив проверок)")
    ] = False,
    clean_orphans: Annotated[
        bool,
        typer.Option(
            "--clean-orphans",
            help="Удалить осиротевшие docker-ресурсы svarog-agent (без живого owner-pid)",
        ),
    ] = False,
) -> None:
    """Диагностика окружения: конфиг, БД, git, sandbox, ключи, ripgrep (read-only).

    Единственное исключение из read-only — явный --clean-orphans: чистка
    docker-ресурсов svarog-agent без живого владельца (legacy до reaper'а).
    """
    from svarog_harness.cli.doctor import (
        collect_checks,
        find_agent_orphans,
        remove_agent_orphans,
    )

    if clean_orphans:
        containers, networks = find_agent_orphans()
        if not containers and not networks:
            console.print("осиротевших ресурсов svarog-agent нет")
        else:
            remove_agent_orphans(containers, networks)
            console.print(f"удалено: {', '.join(containers + networks)}")
    checks = collect_checks(Path.cwd())
    if json_output:
        print(json.dumps([c.to_dict() for c in checks], ensure_ascii=False, indent=2))
    else:
        for check in checks:
            style = _DOCTOR_STYLE[check.status]
            line = f"[{style}]{check.status:4}[/{style}] {check.name}: {check.detail}"
            console.print(line)
            if check.hint and check.status != "ok":
                console.print(f"      [dim]→ {check.hint}[/dim]")
    if any(check.status == "fail" for check in checks):
        raise typer.Exit(code=1)


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
    executor: Annotated[
        str | None,
        typer.Option(
            "--executor",
            help="Активный исполнитель: native (по умолчанию) | claude-code | opencode",
        ),
    ] = None,
    claude_auth: Annotated[
        str | None,
        typer.Option("--claude-auth", help="Режим авторизации Claude Code: api-key | subscription"),
    ] = None,
    claude_api_key: Annotated[
        str | None, typer.Option("--claude-api-key", help="Anthropic API-ключ (auth=api-key)")
    ] = None,
    claude_oauth_token: Annotated[
        str | None,
        typer.Option(
            "--claude-oauth-token",
            help="OAuth-токен подписки (`claude setup-token`, auth=subscription)",
        ),
    ] = None,
    opencode_model: Annotated[
        str | None, typer.Option("--opencode-model", help="Модель для OpenCode")
    ] = None,
    opencode_base_url: Annotated[
        str | None, typer.Option("--opencode-base-url", help="Base URL endpoint для OpenCode")
    ] = None,
    opencode_api_key: Annotated[
        str | None, typer.Option("--opencode-api-key", help="API-ключ для OpenCode")
    ] = None,
    opencode_same_as_native: Annotated[
        bool,
        typer.Option(
            "--opencode-same-as-native",
            help="OpenCode использует те же креды (модель/base_url/ключ), что и нативный provider",
        ),
    ] = False,
    opencode_own_creds: Annotated[
        bool,
        typer.Option("--opencode-own-creds", help="OpenCode настраивается отдельными кредами"),
    ] = False,
) -> None:
    """Создать agent-home: skills, memory (Flow A), policies, .gitignore (§8).

    Без --no-input задаёт интерактивные вопросы (путь, модель, base_url, ключ,
    Claude Code, OpenCode).
    """
    interactive = not no_input and sys.stdin.isatty()

    if path is None and interactive:
        path = Path(typer.prompt("Каталог agent-home", default="./agent-home"))
    target = (path or Path.cwd() / "agent-home").expanduser().resolve()

    existing_cfg: SvarogConfig | None = None
    config_path = target / "svarog.yaml"
    if config_path.is_file():
        try:
            # `init` настраивает именно agent-home, поэтому user-конфиг не
            # должен подменять его значения в подсказках.
            existing_cfg = load_config(
                project_dir=target, user_config_path=target / ".no-user-config.yaml"
            )
        except ConfigError as exc:
            console.print(
                f"[yellow]не удалось прочитать существующий {config_path.name}: {exc}[/yellow]"
            )

    existing_model: str | None = None
    existing_base_url: str | None = None
    existing_api_key_ref: str | None = None
    existing_api_key_is_set = False
    if existing_cfg is not None:
        provider = existing_cfg.models.providers[existing_cfg.models.default]
        existing_model = provider.model
        existing_base_url = provider.base_url
        existing_api_key_ref = provider.api_key_ref
        if existing_api_key_ref is not None:
            store = default_secret_store(
                existing_cfg.secrets.path, env_fallback=existing_cfg.secrets.env_fallback
            )
            existing_api_key_is_set = store.get(existing_api_key_ref) is not None

    if interactive:
        if model is None:
            model = typer.prompt("Модель", default=existing_model or DEFAULT_MODEL)
        if base_url is None:
            base_url = typer.prompt(
                "Base URL endpoint", default=existing_base_url or DEFAULT_BASE_URL
            )
        if api_key is None:
            key_prompt = "API-ключ (Enter — пропустить; для локальной модели не нужен)"
            if existing_api_key_ref is not None:
                key_prompt = "API-ключ (Enter — оставить настроенный ключ)"
            api_key = _prompt_secret(key_prompt) or None
    model = model or existing_model or DEFAULT_MODEL
    base_url = base_url or existing_base_url or DEFAULT_BASE_URL
    api_key_ref = DEFAULT_API_KEY_REF if api_key else existing_api_key_ref

    claude_requested = bool(
        claude_auth or claude_api_key or claude_oauth_token or executor == "claude-code"
    )
    opencode_requested = bool(
        opencode_model
        or opencode_base_url
        or opencode_api_key
        or opencode_same_as_native
        or opencode_own_creds
        or executor == "opencode"
    )

    if interactive and not claude_requested:
        claude_requested = typer.confirm("Настроить Claude Code как исполнителя?", default=True)
    if interactive and not opencode_requested:
        opencode_requested = typer.confirm("Настроить OpenCode как исполнителя?", default=True)

    if claude_requested:
        if interactive and claude_auth is None:
            claude_auth = typer.prompt(
                "Режим авторизации Claude Code (api-key/subscription)", default="subscription"
            )
        # Явно переданный ключ подразумевает API-режим; во всех прочих
        # сценариях Claude Code использует привычную подписку по умолчанию.
        claude_auth = claude_auth or ("api-key" if claude_api_key else "subscription")
        if claude_auth == "subscription":
            if interactive and claude_oauth_token is None:
                claude_oauth_token = (
                    _prompt_secret(
                        "OAuth-токен подписки (`claude setup-token`, Enter — пропустить, "
                        "добавить позже)"
                    )
                    or None
                )
        else:
            if interactive and claude_api_key is None:
                claude_api_key = (
                    _prompt_secret("Anthropic API-ключ (Enter — пропустить, добавить позже)")
                    or None
                )

    opencode_reuse_native = True
    if opencode_requested:
        if opencode_own_creds:
            opencode_reuse_native = False
        elif opencode_same_as_native:
            opencode_reuse_native = True
        elif interactive:
            opencode_reuse_native = typer.confirm(
                "OpenCode: использовать те же креды, что и у нативного provider'а?",
                default=True,
            )
            if opencode_reuse_native:
                opencode_same_as_native = True
            else:
                opencode_own_creds = True
        else:
            opencode_reuse_native = True
            opencode_same_as_native = True

        if not opencode_reuse_native:
            if interactive and opencode_model is None:
                opencode_model = typer.prompt("Модель для OpenCode", default=model)
            if interactive and opencode_base_url is None:
                opencode_base_url = typer.prompt("Base URL endpoint для OpenCode", default=base_url)
            if interactive and opencode_api_key is None:
                opencode_api_key = (
                    _prompt_secret("API-ключ для OpenCode (Enter — пропустить, добавить позже)")
                    or None
                )

    if interactive and claude_requested and opencode_requested and executor is None:
        choice = typer.prompt(
            "Какой сделать активным исполнителем (claude-code/opencode)",
            default="claude-code",
        ).strip()
        executor = choice if choice in ("claude-code", "opencode") else "claude-code"

    claude_answers = ClaudeAnswers(
        requested=claude_requested,
        auth=claude_auth or ("api-key" if claude_api_key else "subscription"),
        api_key=claude_api_key,
        oauth_token=claude_oauth_token,
    )
    opencode_answers = OpencodeAnswers(
        requested=opencode_requested,
        same_as_native=opencode_same_as_native,
        own_creds=opencode_own_creds,
        model=opencode_model,
        base_url=opencode_base_url,
        api_key=opencode_api_key,
    )
    try:
        executor_setup = resolve_executor_setup(
            executor=executor,
            claude=claude_answers,
            opencode=opencode_answers,
            native_model=model,
            native_base_url=base_url,
            native_api_key_ref=api_key_ref,
        )
    except ExecutorSetupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    if executor_setup is not None:
        adapters: list[ExecutorAdapter] = [executor_setup.active]
        if executor_setup.claude is not None and executor_setup.active != "claude-code":
            adapters.append("claude-code")
        if executor_setup.opencode is not None and executor_setup.active != "opencode":
            adapters.append("opencode")
        for adapter in adapters:
            console.print(f"[dim]собираю образ {adapter}…[/dim]")
            try:
                image = build_executor_image(
                    adapter,
                    on_progress=lambda line: console.print(f"[dim]{line}[/dim]"),
                )
            except ExecutorImageBuildError as exc:
                console.print(f"[red]не удалось подготовить образ executor:[/red] {exc}")
                raise typer.Exit(code=1) from None
            console.print(f"[green]образ готов[/green] ({image})")

    result = scaffold_agent_home(
        target,
        force=force,
        model=model,
        base_url=base_url,
        api_key_ref=api_key_ref,
        executor=executor_setup,
    )
    if existing_cfg is not None and not force:
        models_patch: dict[str, object] = {
            "base_url": base_url,
            "model": model,
        }
        if api_key_ref is not None:
            models_patch["api_key_ref"] = api_key_ref
        config_patch: dict[str, object] = {
            "models": {
                "providers": {
                    existing_cfg.models.default: models_patch,
                }
            }
        }
        if executor_setup is not None:
            config_patch.update(executor_setup_yaml_patch(executor_setup))
        patch_project_config(target, config_patch)
        console.print(f"[green]настройки сохранены[/green] в {config_path.name}")
    for created in result.created:
        console.print(f"[green]+[/green] {created.relative_to(target)}")
    for skipped in result.skipped:
        console.print(f"[dim]= {skipped.relative_to(target)} (существует, пропущено)[/dim]")

    secrets_to_store: list[tuple[str, str, str]] = []
    if api_key and api_key_ref:
        secrets_to_store.append((api_key_ref, api_key, "модели"))
    if executor_setup is not None and executor_setup.claude is not None:
        claude_setup = executor_setup.claude
        if claude_setup.auth == "api-key" and claude_api_key and claude_setup.api_key_ref:
            secrets_to_store.append((claude_setup.api_key_ref, claude_api_key, "Claude Code"))
        if (
            claude_setup.auth == "subscription"
            and claude_oauth_token
            and claude_setup.oauth_token_ref
        ):
            secrets_to_store.append(
                (claude_setup.oauth_token_ref, claude_oauth_token, "Claude Code (OAuth)")
            )
    if executor_setup is not None and executor_setup.opencode is not None:
        opencode_setup = executor_setup.opencode
        if opencode_api_key and opencode_setup.api_key_ref:
            secrets_to_store.append((opencode_setup.api_key_ref, opencode_api_key, "OpenCode"))

    if secrets_to_store:
        secrets_path = SecretsConfig().path
        assert secrets_path is not None  # дефолт схемы всегда задан
        store = FileSecretStore(secrets_path.expanduser())
        for ref, value, label in secrets_to_store:
            store.set(ref, value)
            console.print(f"[green]ключ сохранён[/green] ({label}) в {secrets_path} (ref: {ref})")

    ignored = _ensure_gitignored(target)
    if ignored is not None:
        console.print(f"[dim]agent-home добавлен в {ignored}[/dim]")

    async def init_git_subrepo(path: Path, message: str) -> None:
        repo = GitRepo(path)
        if not await repo.is_repo():
            # separate-git-dir по умолчанию (ADR-0015 §0.2): объекты git —
            # вне дерева репозитория, недостижимы из-под агента.
            await repo.init(separate_git_dir=separate_gitdir_for(path))
            await repo.ensure_identity()
            await repo.add_all()
            with contextlib.suppress(GitError):
                await repo.commit(message)

    async def init_subrepos() -> None:
        # memory — Flow A (ADR-0004); skills — Flow B базовая ветка для proposals (§18).
        await init_git_subrepo(target / "memory", "svarog init: memory repo")
        await init_git_subrepo(target / "skills", "svarog init: skills repo")

    asyncio.run(init_subrepos())

    pending_refs: list[str] = []
    if api_key_ref and not api_key and not existing_api_key_is_set:
        pending_refs.append(api_key_ref)
    if executor_setup is not None and executor_setup.claude is not None:
        claude_setup = executor_setup.claude
        if claude_setup.auth == "api-key" and claude_setup.api_key_ref and not claude_api_key:
            pending_refs.append(claude_setup.api_key_ref)
        if (
            claude_setup.auth == "subscription"
            and claude_setup.oauth_token_ref
            and not claude_oauth_token
        ):
            pending_refs.append(claude_setup.oauth_token_ref)
    if (
        executor_setup is not None
        and executor_setup.opencode is not None
        and not opencode_reuse_native
        and executor_setup.opencode.api_key_ref
        and not opencode_api_key
    ):
        pending_refs.append(executor_setup.opencode.api_key_ref)

    if pending_refs:
        reminders = ", ".join(f"`svarog secrets set {ref}`" for ref in pending_refs)
        next_step = f"добавьте ключи: {reminders}"
    else:
        next_step = 'запустите `svarog run "…"`'
    executor_note = f"; исполнитель: {executor_setup.active}" if executor_setup is not None else ""
    console.print(
        f"\n[bold]agent-home готов:[/bold] {target}\n"
        f"[dim]модель {model} @ {base_url}{executor_note}; {next_step}[/dim]"
    )


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

    def on_memory_proposal(proposal: MemoryProposal) -> None:
        if proposal.status.value == "pending":
            console.print(
                f"[cyan]memory proposal[/cyan] {proposal.title} "
                f"[dim](review: svarog memory proposals show {proposal.id[:8]})[/dim]"
            )
        else:
            console.print(f"[yellow]memory proposal {proposal.title}: отклонён валидацией[/yellow]")
            for message in MemoryProposalManager.validation_messages(proposal):
                console.print(f"[yellow]  - {message}[/yellow]")

    def on_progress(
        iterations: int, tokens: int, cost: float, context_ratio: float, cached: int
    ) -> None:
        # Cost/context-индикатор (ADR-0015 фаза 5): одна dim-строка на итерацию.
        cached_suffix = f", кэш {cached}" if cached > 0 else ""
        console.print(
            f"\n[dim]итерация {iterations} | {tokens} ток.{cached_suffix} | ${cost:.4f} | "
            f"контекст {context_ratio:.0%}[/dim]",
            highlight=False,
        )

    return RunHooks(
        on_skill_skipped=lambda name, reason: console.print(
            f"[yellow]skill пропущен ({name}): {reason}[/yellow]"
        ),
        on_workspace_prep=on_prep,
        on_recovered=on_recovered,
        on_progress=on_progress,
        on_text_delta=lambda delta: console.print(delta, end="", highlight=False),
        on_tool_call=lambda name, args: console.print(format_tool_call(name, args)),
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
        on_memory_proposal=on_memory_proposal,
    )


def _confirm_layout_overlap(cfg: SvarogConfig, workspace: Path) -> bool:
    """Запуск из любой папки (ADR-0018): пересечение workspace с control-plane —
    только с явного подтверждения человека.

    True — человек подтвердил (гейт ADR-0015 §0.3 пропустит), False — нечего
    подтверждать или нет TTY (тогда решает fail-closed гейт в рантайме).
    Отказ в промпте завершает команду сразу.
    """
    if not _stdio_is_tty():
        return False
    violations = workspace_layout_violations(cfg, workspace)
    if not violations or cfg.sandbox.type == "local-trusted":
        return False
    console.print("[yellow]workspace затрагивает control-plane Svarog:[/yellow]")
    for violation in violations:
        console.print(f"[yellow]  - {violation}[/yellow]")
    console.print(
        "[yellow]агент сможет читать и менять код, память, скиллы и настройки "
        "самого Svarog (ADR-0015 §0.3)[/yellow]"
    )
    if not typer.confirm("продолжить в этом workspace?", default=False):
        raise typer.Exit(code=1)
    return True


async def _run_task(
    cfg: SvarogConfig,
    workspace: Path,
    task: str,
    autonomy: AutonomyMode,
    hooks: RunHooks,
    *,
    allow_layout_overlap: bool = False,
) -> RunOutcome:
    runner = TaskRunner(cfg, workspace, allow_layout_overlap=allow_layout_overlap)
    return await runner.run_once(task, autonomy, hooks=hooks)


async def _resume_task(
    cfg: SvarogConfig, run_id: str, hooks: RunHooks, *, allow_layout_overlap: bool = False
) -> RunOutcome:
    runner = TaskRunner(cfg, Path.cwd().resolve(), allow_layout_overlap=allow_layout_overlap)
    return await runner.resume(run_id, hooks=hooks)


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
    json_output: Annotated[
        bool, typer.Option("--json", help="Итог одним JSON-объектом, без интерактива")
    ] = False,
) -> None:
    """Выполнить задачу агентом в workspace (один agent run)."""
    workspace = (workspace or Path.cwd()).resolve()
    if not workspace.is_dir():
        console.print(f"[red]workspace не существует:[/red] {workspace}")
        raise typer.Exit(code=1)
    cfg = _load_config_or_exit(project_dir=workspace)
    autonomy = _resolve_autonomy(cfg, yolo=yolo, auto=auto, supervised=supervised)

    if not json_output:
        console.print(f"[bold]задача:[/bold] {task}")
        console.print(f"[dim]workspace: {workspace} | автономия: {autonomy.value}[/dim]\n")
    hooks = RunHooks() if json_output else _console_hooks()
    # --json неинтерактивен: пересечение с control-plane остаётся fail-closed.
    allow_overlap = False if json_output else _confirm_layout_overlap(cfg, workspace)
    try:
        outcome = asyncio.run(
            _run_task(cfg, workspace, task, autonomy, hooks, allow_layout_overlap=allow_overlap)
        )
    except ApiKeyError as exc:
        console.print(f"[red]ошибка доступа к модели:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SandboxError as exc:
        console.print(f"[red]ошибка sandbox:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except WorkspaceLayoutError as exc:
        console.print(f"[red]ошибка раскладки workspace:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except WorkspaceBusyError as exc:
        console.print(f"[red]workspace занят:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except PolicyRulesError as exc:
        console.print(f"[red]ошибка policy-правил:[/red] {exc}")
        raise typer.Exit(code=1) from None
    if not json_output:
        # Машиночитаемый режим не интерактивен: approval решается отдельными
        # командами `svarog approvals approve/deny` + `resume`.
        outcome = _interactive_approvals(cfg, outcome, allow_layout_overlap=allow_overlap)
    _report_outcome(outcome, _failed_checks(cfg, outcome), as_json=json_output)


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
    session: Annotated[
        str | None,
        typer.Option("--session", help="Продолжить сессию по id/префиксу (см. sessions list)"),
    ] = None,
    fork: Annotated[
        str | None,
        typer.Option("--fork", help="Новая сессия с копией истории указанной (id/префикс)"),
    ] = None,
    plain: Annotated[
        bool, typer.Option("--plain", help="Построчный REPL без живой области стрима")
    ] = False,
) -> None:
    """Интерактивная сессия: каждое сообщение — run в общей session (§10.1).

    На TTY по умолчанию — inline-режим (ADR-0018): диалог в обычном буфере
    терминала (scrollback, нативное выделение и копирование), живая область
    только у текущего ответа. --plain или отсутствие терминала (pipe, CI) —
    построчный REPL.
    """
    if session is not None and fork is not None:
        console.print("[red]--session и --fork взаимоисключающие[/red]")
        raise typer.Exit(code=1)
    workspace = (workspace or Path.cwd()).resolve()
    if not workspace.is_dir():
        console.print(f"[red]workspace не существует:[/red] {workspace}")
        raise typer.Exit(code=1)
    cfg = _load_config_or_exit(project_dir=workspace)
    autonomy = _resolve_autonomy(cfg, yolo=yolo, auto=auto, supervised=supervised)
    allow_overlap = _confirm_layout_overlap(cfg, workspace)
    try:
        if not plain and _stdio_is_tty():
            # Inline-режим (qwen-code-style): scrollback + живая область ответа.
            from svarog_harness.cli.chat_inline import run_chat_inline

            hooks = _console_hooks()
            hooks.on_approval_requested = lambda approval: _prompt_gate_decision(cfg, approval)
            asyncio.run(
                run_chat_inline(
                    cfg,
                    workspace,
                    autonomy,
                    hooks,
                    continue_ref=session,
                    fork_ref=fork,
                    allow_layout_overlap=allow_overlap,
                )
            )
            return
        console.print(
            f"[bold]svarog chat[/bold] | workspace: {workspace} | автономия: {autonomy.value}\n"
            f"[dim]пустая строка или /quit — выход[/dim]"
        )
        asyncio.run(
            _chat_session(
                cfg,
                workspace,
                autonomy,
                continue_ref=session,
                fork_ref=fork,
                allow_layout_overlap=allow_overlap,
            )
        )
    except SessionNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    except ApiKeyError as exc:
        console.print(f"[red]ошибка доступа к модели:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SandboxError as exc:
        console.print(f"[red]ошибка sandbox:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except WorkspaceLayoutError as exc:
        console.print(f"[red]ошибка раскладки workspace:[/red] {exc}")
        raise typer.Exit(code=1) from None


def _stdio_is_tty() -> bool:
    """TTY-автовыбор TUI (ADR-0018): оба конца — терминал."""
    return sys.stdin.isatty() and sys.stdout.isatty()


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


async def _chat_session(
    cfg: SvarogConfig,
    workspace: Path,
    autonomy: AutonomyMode,
    *,
    continue_ref: str | None = None,
    fork_ref: str | None = None,
    allow_layout_overlap: bool = False,
) -> None:
    hooks = _console_hooks()
    if sys.stdin.isatty():
        # Живой промпт approval/ask_user прямо в чате (§7): решение уходит в
        # БД, гейт продолжает агента без suspend/resume.
        hooks.on_approval_requested = lambda approval: _prompt_gate_decision(cfg, approval)
    engine_ctx = ChatEngine(
        cfg, workspace, autonomy, hooks, allow_layout_overlap=allow_layout_overlap
    )
    async with engine_ctx as engine:
        start = await engine.start(continue_ref=continue_ref, fork_ref=fork_ref)
        if start.label:
            console.print(f"[dim]{start.label}[/dim]")
        while True:
            try:
                task = (
                    await asyncio.to_thread(_read_user_line, "\n[bold cyan]› [/bold cyan]")
                ).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not task or task in {"/quit", "/exit"}:
                break
            outcome = await engine.send(task)
            _print_chat_turn(outcome)


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
    json_output: Annotated[
        bool, typer.Option("--json", help="Итог одним JSON-объектом, без интерактива")
    ] = False,
) -> None:
    """Возобновить suspended run из checkpoint (ADR-0005)."""
    cfg = _load_config_or_exit()
    hooks = RunHooks() if json_output else _console_hooks()
    allow_overlap = False
    try:
        try:
            outcome = asyncio.run(_resume_task(cfg, run_id, hooks))
        except WorkspaceLayoutError:
            # Workspace checkpoint'а известен только после загрузки — поэтому
            # подтверждение пересечения с control-plane (ADR-0018) здесь
            # запрашивается по факту отказа гейта, а не заранее.
            if json_output or not _stdio_is_tty():
                raise
            console.print(
                "[yellow]workspace run'а затрагивает control-plane Svarog — агент "
                "сможет читать и менять код/настройки самого Svarog (ADR-0015 §0.3)[/yellow]"
            )
            if not typer.confirm("продолжить resume в этом workspace?", default=False):
                raise typer.Exit(code=1) from None
            allow_overlap = True
            outcome = asyncio.run(_resume_task(cfg, run_id, hooks, allow_layout_overlap=True))
    except WorkspaceLayoutError as exc:
        console.print(f"[red]ошибка раскладки workspace:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except ConfigDriftError as exc:
        console.print(f"[red]resume отклонён (изменился security-конфиг):[/red] {exc}")
        raise typer.Exit(code=1) from None
    except WorkspaceBusyError as exc:
        console.print(f"[red]workspace занят:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except (RunNotFoundError, RunNotResumableError, ConfigError, PolicyRulesError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    except ApiKeyError as exc:
        console.print(f"[red]ошибка доступа к модели:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SandboxError as exc:
        console.print(f"[red]ошибка sandbox:[/red] {exc}")
        raise typer.Exit(code=1) from None
    if not json_output:
        outcome = _interactive_approvals(cfg, outcome, allow_layout_overlap=allow_overlap)
    _report_outcome(outcome, _failed_checks(cfg, outcome), as_json=json_output)


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


def _pick_option_sync(
    title: str, values: list[tuple[str, str]], default: str | None = None
) -> str | None:
    """Radiolist (↑↓ + Enter) из sync-контекста; None — Esc или нет tty.

    Гейт-промпты вызываются из worker-потока bridge'а (§7) — свой event loop
    через asyncio.run безопасен; при любом сбое терминала вызывающий обязан
    откатиться на текстовый промпт.
    """
    from svarog_harness.cli.chat_picker import pick_option

    try:
        return asyncio.run(pick_option(title, values, default=default))
    except Exception:
        return None


# Сентинел пункта «свой ответ…» в списке вариантов ask_user (не пересекается
# с текстом вариантов — управляющий символ в ответах модели невозможен).
_CUSTOM_ANSWER = "\x00custom"


def _confirm_approval(cfg: SvarogConfig, approval: Approval, *, decided_by: str) -> None:
    """Показать действие и записать вердикт человека из терминала."""
    _show_approval(approval)
    choice = _pick_option_sync(
        "approval",
        [
            ("approve", "Одобрить"),
            ("deny", "Отклонить"),
            ("deny-reason", "Отклонить с причиной…"),
        ],
        default="deny",
    )
    if choice is None:
        # Esc или терминал без диалогов — классический y/n.
        approved = typer.confirm("одобрить действие?", default=False)
        reason = None
        if not approved:
            reason = typer.prompt("причина отказа", default="", show_default=False) or None
    else:
        approved = choice == "approve"
        reason = None
        if choice == "deny-reason":
            reason = typer.prompt("причина отказа", default="", show_default=False) or None
    record_gate_decision(cfg, approval.id, approved=approved, reason=reason, decided_by=decided_by)


def _prompt_gate_decision(cfg: SvarogConfig, approval: Approval) -> None:
    """Живой промпт гейта прямо в chat (§7) — как permission-prompt Claude Code.

    Вызывается bridge'ом в worker-потоке во время grace-ожидания: блокирующий
    stdin здесь допустим, решение пишется в БД той же механикой, что у
    `svarog approvals` из второго терминала, — poll гейта подхватит его и
    агент продолжит без suspend/resume.
    """
    console.print()
    if approval.action_type == "user.question":
        _answer_question_interactive(cfg, approval)
        return
    _confirm_approval(cfg, approval, decided_by="chat")


def _interactive_approvals(
    cfg: SvarogConfig, outcome: RunOutcome, *, allow_layout_overlap: bool = False
) -> RunOutcome:
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
            _confirm_approval(cfg, approval, decided_by="cli")
        outcome = asyncio.run(
            _resume_task(
                cfg, outcome.run_id, _console_hooks(), allow_layout_overlap=allow_layout_overlap
            )
        )
    return outcome


def _answer_question_interactive(cfg: SvarogConfig, approval: Approval) -> None:
    """ask_user: показать вопрос и записать ответ (§6.5).

    Если агент передал options — показать их radiolist'ом (↑↓ + Enter) с
    пунктом «свой ответ…»; иначе (или при Esc/недоступном диалоге) —
    свободный текстовый промпт.
    """
    payload = approval.payload or {}
    console.print(f"[bold]вопрос {approval.id[:8]}[/bold] | run {approval.run_id[:8]}")
    console.print(f"  [cyan]{payload.get('question') or payload.get('reason') or ''}[/cyan]")
    options = [o for o in payload.get("options") or [] if isinstance(o, str) and o.strip()]
    answer: str | None = None
    if options:
        values = [(o, o) for o in options] + [(_CUSTOM_ANSWER, "свой ответ…")]
        choice = _pick_option_sync("вопрос агента", values)
        if choice is not None and choice != _CUSTOM_ANSWER:
            answer = choice
    if answer is None:
        answer = typer.prompt(
            "ваш ответ (Enter — продолжить без ответа)", default="", show_default=False
        )
    record_gate_answer(cfg, approval.id, answer, answered_by="cli")


def _outcome_exit_code(outcome: RunOutcome, failed_checks: int) -> int:
    """Единая карта exit-кодов для человека и --json (§10.1)."""
    if outcome.state is RunState.COMPLETED:
        return 4 if failed_checks else 0
    if outcome.state in (RunState.SUSPENDED, RunState.WAITING_APPROVAL):
        return 3
    if outcome.error and outcome.error.startswith("verifier:"):
        return 4
    return 2


def _report_outcome(outcome: RunOutcome, failed_checks: int = 0, *, as_json: bool = False) -> None:
    if as_json:
        payload = {
            "run_id": outcome.run_id,
            "state": outcome.state.value,
            "final_answer": outcome.final_answer,
            "error": outcome.error,
            "iterations": outcome.iterations,
            "tokens_used": outcome.tokens_used,
            "cost_usd": outcome.cost_usd,
            "failed_checks": failed_checks,
        }
        print(json.dumps(payload, ensure_ascii=False))
        code = _outcome_exit_code(outcome, failed_checks)
        if code:
            raise typer.Exit(code=code)
        return
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
    json_output: Annotated[
        bool, typer.Option("--json", help="NDJSON: по одному JSON-объекту на строку")
    ] = False,
) -> None:
    """Показать последние runs."""
    cfg = _load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        runs = await fetch_runs(db, limit=limit)
        if json_output:
            for run in runs:
                print(json.dumps(run_to_dict(run), ensure_ascii=False))
            return
        if not runs:
            console.print('runs пока нет — запустите `svarog run "задача"`')
            return
        console.print(render_runs_table(runs))

    asyncio.run(_with_db(cfg, action))


@traces_app.command("show")
def traces_show(
    run_id: Annotated[str, typer.Argument(help="id run'а или его уникальный префикс")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Полный trace одним JSON-объектом")
    ] = False,
) -> None:
    """Показать полный trace одного run."""
    cfg = _load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        try:
            run, messages, tool_calls, checks = await fetch_run(db, run_id)
        except RunNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        if json_output:
            detail = run_detail_to_dict(run, messages, tool_calls, checks)
            print(json.dumps(detail, ensure_ascii=False, indent=2))
            return
        console.print(render_run(run, messages, tool_calls, checks))

    asyncio.run(_with_db(cfg, action))


@sessions_app.command("list")
def sessions_list(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Сколько сессий показать")] = 20,
    search: Annotated[
        str | None, typer.Option("--search", help="Подстрока в названии или задачах runs")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="NDJSON: по одному JSON-объекту на строку")
    ] = False,
) -> None:
    """Сессии от свежих к старым (продолжить: chat --session <id>)."""
    cfg = _load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        summaries = await fetch_sessions(db, limit=limit, search=search)
        if json_output:
            for summary in summaries:
                print(json.dumps(session_to_dict(summary), ensure_ascii=False))
            return
        if not summaries:
            console.print("сессий не найдено")
            return
        console.print(render_sessions_table(summaries))

    asyncio.run(_with_db(cfg, action))


@sessions_app.command("rename")
def sessions_rename(
    session_id: Annotated[str, typer.Argument(help="id сессии или её префикс")],
    title: Annotated[str, typer.Argument(help="Новое название")],
) -> None:
    """Переименовать сессию."""
    cfg = _load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        session = await find_session_by_prefix(db, session_id)
        await TraceRecorder(db).rename_session(session, title)
        console.print(f"[green]сессия {session.id[:8]} → «{title}»[/green]")

    try:
        asyncio.run(_with_db(cfg, action))
    except SessionNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None


@app.command()
def rewind(
    run_id: Annotated[str, typer.Argument(help="id run'а или его префикс")],
    yes: Annotated[bool, typer.Option("--yes", help="Не спрашивать подтверждение")] = False,
) -> None:
    """Откатить workspace к состоянию до run'а (turn-level rewind, git reset --hard)."""
    cfg = _load_config_or_exit()
    workspace = Path.cwd().resolve()
    repo = GitRepo(workspace)

    async def resolve_run(db: AsyncSession) -> str:
        return (await find_run_by_prefix(db, run_id)).id

    async def plan_rewind(full_id: str) -> tuple[str, list[str]]:
        """(sha цели, отбрасываемые коммиты); границы — только свои step-коммиты."""
        if not await repo.is_repo():
            raise GitError("workspace не является git-репозиторием")
        if await repo.is_dirty():
            raise GitError(
                "в workspace незакоммиченные изменения — закоммитьте или уберите их перед rewind"
            )
        rows = await repo.log_with_run_ids()
        indexed = [i for i, (_, rid) in enumerate(rows) if rid == full_id]
        if not indexed:
            raise GitError(f"в текущей ветке нет step-коммитов run {full_id[:8]}")
        oldest = max(indexed)
        foreign = [sha[:8] for sha, rid in rows[: oldest + 1] if rid != full_id]
        if foreign:
            raise GitError(
                f"поверх step-коммитов run {full_id[:8]} лежат чужие коммиты "
                f"({', '.join(foreign)}) — rewind отменён, откатите их отдельно"
            )
        if oldest + 1 >= len(rows):
            raise GitError("step-коммит run'а — корневой коммит: откатывать не к чему")
        target = rows[oldest + 1][0]
        dropped = [sha[:8] for sha, _ in rows[: oldest + 1]]
        return target, dropped

    try:
        full_id = asyncio.run(_with_db(cfg, resolve_run))
        target, dropped = asyncio.run(plan_rewind(full_id))
    except (RunNotFoundError, GitError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    if not yes:
        console.print(f"будут отброшены коммиты: {', '.join(dropped)}")
        typer.confirm("выполнить git reset --hard?", abort=True)
    asyncio.run(repo.reset_hard(target))
    console.print(
        f"[green]workspace откачен[/green] к {target[:8]} "
        f"(отброшено коммитов: {len(dropped)}, run {full_id[:8]})"
    )


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


cron_app = typer.Typer(help="Джобы планировщика (ADR-0019).", no_args_is_help=True)
app.add_typer(cron_app, name="cron")


def _job_row(job: CronJob) -> dict[str, object]:
    return {
        "id": job.id,
        "name": job.name,
        "schedule": f"{job.schedule_kind.value}:{job.schedule_spec}",
        "tz": job.tz,
        "enabled": job.enabled,
        "protected": job.protected,
        "origin": job.origin.value,
        "autonomy": job.autonomy,
        "next_run_at": job.next_run_at.isoformat(),
        "last_status": job.last_status,
        "run_count": job.run_count,
    }


@cron_app.command("add")
def cron_add(
    name: Annotated[str, typer.Argument(help="Имя джобы")],
    task: Annotated[str, typer.Option("--task", help="Задача для агента")],
    every: Annotated[str | None, typer.Option("--every", help="Интервал в секундах")] = None,
    at: Annotated[str | None, typer.Option("--at", help="Время суток HH:MM")] = None,
    tz: Annotated[str, typer.Option("--tz", help="Таймзона расписания")] = "UTC",
    workspace: Annotated[
        Path | None, typer.Option("--workspace", "-w", help="Рабочая директория джобы")
    ] = None,
    yolo: Annotated[bool, typer.Option("--yolo", help="Режим автономии yolo")] = False,
    auto: Annotated[bool, typer.Option("--auto", help="Режим автономии auto")] = False,
    supervised: Annotated[
        bool, typer.Option("--supervised", help="Режим автономии supervised")
    ] = False,
) -> None:
    """Завести джобу. Создаётся ВЫКЛЮЧЕННОЙ: включает `cron enable`."""
    if (every is None) == (at is None):
        console.print("[red]укажите ровно одно расписание:[/red] --every ИЛИ --at")
        raise typer.Exit(code=1)
    workspace = (workspace or Path.cwd()).resolve()
    cfg = _load_config_or_exit(project_dir=workspace)
    autonomy = _resolve_autonomy(cfg, yolo=yolo, auto=auto, supervised=supervised)
    kind = ScheduleKind.EVERY if every is not None else ScheduleKind.DAILY_AT
    spec = every if every is not None else at
    assert spec is not None  # гарантировано проверкой выше
    try:
        parse_spec(kind, spec)
        first = next_run_after(kind, spec, tz, utcnow())
    except ScheduleSpecError as exc:
        console.print(f"[red]расписание отклонено:[/red] {exc}")
        raise typer.Exit(code=1) from None

    async def action(db: AsyncSession) -> None:
        job = await JobStore(db).create(
            name=name,
            kind=kind,
            spec=spec,
            tz=tz,
            task=task,
            workspace=str(workspace),
            autonomy=autonomy.value,
            # Права замораживаются здесь: ослабление конфига позже не повысит
            # прав уже заведённой джобы (ADR-0019).
            config_digest=config_digest(cfg, workspace),
            origin=JobOrigin.HUMAN,
            first_run_at=first,
        )
        console.print(
            f"джоба [bold]{job.name}[/bold] ({job.id[:8]}) создана и пока "
            f"[yellow]выключена[/yellow]; включить: svarog cron enable {job.id[:8]}"
        )

    asyncio.run(_with_db(cfg, action))


@cron_app.command("list")
def cron_list(
    json_output: Annotated[
        bool, typer.Option("--json", help="NDJSON: по одному объекту на строку")
    ] = False,
) -> None:
    """Показать джобы планировщика."""
    cfg = _load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        jobs = await JobStore(db).list_jobs()
        if json_output:
            for job in jobs:
                print(json.dumps(_job_row(job), ensure_ascii=False))
            return
        if not jobs:
            console.print(
                'джоб пока нет — заведите `svarog cron add <имя> --task "…" --every 3600`'
            )
            return
        table = Table(title="Джобы планировщика")
        for column in (
            "id",
            "имя",
            "расписание",
            "включена",
            "автономия",
            "следующий запуск",
            "статус",
        ):
            table.add_column(column)
        for job in jobs:
            table.add_row(
                job.id[:8],
                job.name + (" [системная]" if job.protected else ""),
                f"{job.schedule_kind.value}:{job.schedule_spec} {job.tz}",
                "да" if job.enabled else "нет",
                job.autonomy,
                job.next_run_at.isoformat(timespec="minutes"),
                job.last_status or "—",
            )
        console.print(table)

    asyncio.run(_with_db(cfg, action))


async def _resolve_job(db: AsyncSession, job_id: str) -> CronJob:
    """Найти джобу по полному id или уникальному префиксу."""
    store = JobStore(db)
    matches = [job for job in await store.list_jobs() if job.id.startswith(job_id)]
    if not matches:
        raise JobNotFoundError(f"джоба не найдена: {job_id}")
    if len(matches) > 1:
        raise JobNotFoundError(f"префикс {job_id!r} неоднозначен: {len(matches)} джоб")
    return matches[0]


def _cron_toggle(job_id: str, *, enabled: bool) -> None:
    cfg = _load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        try:
            job = await _resolve_job(db, job_id)
        except JobNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        await JobStore(db).set_enabled(job, enabled)
        state = "включена" if enabled else "выключена"
        console.print(f"джоба [bold]{job.name}[/bold] {state}")

    asyncio.run(_with_db(cfg, action))


@cron_app.command("enable")
def cron_enable(job_id: Annotated[str, typer.Argument(help="id джобы или префикс")]) -> None:
    """Включить джобу."""
    _cron_toggle(job_id, enabled=True)


@cron_app.command("disable")
def cron_disable(job_id: Annotated[str, typer.Argument(help="id джобы или префикс")]) -> None:
    """Выключить джобу."""
    _cron_toggle(job_id, enabled=False)


@cron_app.command("show")
def cron_show(job_id: Annotated[str, typer.Argument(help="id джобы или префикс")]) -> None:
    """Показать джобу целиком."""
    cfg = _load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        try:
            job = await _resolve_job(db, job_id)
        except JobNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        print(json.dumps(_job_row(job) | {"task": job.task}, ensure_ascii=False, indent=2))

    asyncio.run(_with_db(cfg, action))


@cron_app.command("remove")
def cron_remove(job_id: Annotated[str, typer.Argument(help="id джобы или префикс")]) -> None:
    """Удалить джобу. Системные джобы удалить нельзя."""
    cfg = _load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        try:
            job = await _resolve_job(db, job_id)
        except JobNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        try:
            await JobStore(db).remove(job)
        except ProtectedJobError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        console.print(f"джоба [bold]{job.name}[/bold] удалена")

    asyncio.run(_with_db(cfg, action))


async def _dream_blocked(cfg: SvarogConfig, db: AsyncSession) -> str | None:
    """Причина не запускать Dream сейчас, или None.

    Потолок непросмотренных предложений — предохранитель от бесконечного
    накопления: без него ежедневная джоба при неактивном человеке копит мусор
    и тратит токены впустую.
    """
    mem_dir = memory_dir(cfg)
    if mem_dir is None or not mem_dir.is_dir():
        return "пропущено: память не настроена"
    pending = await MemoryProposalManager(db, mem_dir).pending_count()
    if pending >= cfg.dream.max_pending:
        return f"пропущено: {pending} непросмотренных предложений — сначала ревью"
    return None


async def _scheduler_loop(cfg: SvarogConfig, workspace: Path) -> None:
    """Цикл демона: тик, затем пауза (ADR-0019).

    Тик обёрнут межпроцессным локом — первый рубеж против двух одновременно
    запущенных демонов; второй рубеж (compare-and-set по next_run_at) живёт
    внутри JobStore.claim_due.
    """
    digest = config_digest(cfg, workspace)
    lock = default_lock_backend(cfg.storage.db_path)

    async def register_system(db: AsyncSession) -> None:
        created = await ensure_system_jobs(
            JobStore(db),
            workspace=str(workspace),
            autonomy=cfg.runtime.autonomy.value,
            config_digest=digest,
            now=utcnow(),
            prune_interval_sec=cfg.curator.prune_interval_sec,
            dream_enabled=cfg.dream.enabled,
            dream_interval_sec=cfg.dream.interval_sec,
        )
        for job_id in created:
            console.print(f"[dim]заведена системная джоба {job_id[:8]}[/dim]")

    await _with_db(cfg, register_system)

    async def run_job(request: JobRunRequest) -> str:
        task, profile = request.task, RunProfile.DEFAULT
        if request.name == DREAM_JOB_NAME:
            blocked = await _with_db(cfg, lambda db: _dream_blocked(cfg, db))
            if blocked is not None:
                return blocked
            mem_dir = memory_dir(cfg)
            assert mem_dir is not None  # проверено в _dream_blocked
            report = audit_memory(mem_dir, stale_after_days=cfg.curator.stale_after_days)
            task, profile = build_dream_task(report), RunProfile.DREAM

        runner = TaskRunner(cfg, Path(request.workspace))
        outcome = await runner.run_once(
            task, AutonomyMode(request.autonomy), hooks=RunHooks(), profile=profile
        )

        # cron_job_id пишется после run'а: канала для метаданных на старте нет,
        # а расширять сигнатуры трёх слоёв ради одного поля — лишнее.
        async def stamp(db: AsyncSession) -> None:
            run = await db.get(Run, outcome.run_id)
            if run is not None:
                await TraceRecorder(db).merge_run_meta(run, {"cron_job_id": request.job_id})

        await _with_db(cfg, stamp)
        return outcome.state.value

    async def workspace_busy(path: str) -> bool:
        busy = False

        async def check(db: AsyncSession) -> None:
            nonlocal busy
            busy = await TraceRecorder(db).live_run_on_workspace(path) is not None

        await _with_db(cfg, check)
        return busy

    while True:

        async def one_tick(db: AsyncSession) -> None:
            await tick(
                JobStore(db),
                now=utcnow(),
                current_digest=digest,
                run_job=run_job,
                workspace_busy=workspace_busy,
            )

        async with lock.guard(f"scheduler-tick:{workspace}", timeout=5.0) as acquired:
            # Не взяли лок — тикает другой демон; свой проход пропускаем.
            if acquired:
                await _with_db(cfg, one_tick)
        await asyncio.sleep(cfg.scheduler.interval_sec)


@app.command()
def scheduler(
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Рабочая директория (по умолчанию cwd)"),
    ] = None,
) -> None:
    """Демон расписания: исполняет джобы (ADR-0019).

    Отдельный процесс: `svarog serve` джобы НЕ исполняет.
    """
    workspace = (workspace or Path.cwd()).resolve()
    cfg = _load_config_or_exit(project_dir=workspace)
    console.print(
        f"[bold]планировщик запущен[/bold] | интервал {cfg.scheduler.interval_sec}s | "
        f"workspace {workspace}"
    )
    try:
        asyncio.run(_scheduler_loop(cfg, workspace))
    except KeyboardInterrupt:
        console.print("планировщик остановлен")


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
        writer = MemoryWriter(
            db,
            mem_dir,
            lock=default_lock_backend(cfg.storage.db_path),
            index_max_lines=cfg.memory.index_max_lines,
        )
        rows = await writer.drain(known_values=_known_secret_values(cfg, store))
        for row in rows:
            if row.error:
                console.print(f"[yellow]отклонено: {row.error}[/yellow]")
            elif row.commit_sha:
                console.print(f"[green]{row.commit_sha}[/green] применено")
        return len(rows)

    count = asyncio.run(_with_db(cfg, action))
    console.print(f"обработано заявок: {count}")


def _write_memory_audit(workspace: Path, report: MemoryAuditReport) -> Path:
    from datetime import UTC, datetime

    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = artifacts / f"memory-curation-{stamp}.md"
    path.write_text(report.to_markdown(), encoding="utf-8")
    return path


@memory_app.command("curate")
def memory_curate() -> None:
    """Аудит здоровья памяти (ADR-0011): осиротевшие, битые, устаревшие, пустые страницы.

    Детерминированный, только чтение — ничего не мутирует и не блокирует run'ы.
    Находки печатаются и пишутся отчётом в artifacts/.
    """
    cfg = _load_config_or_exit()
    mem_dir = memory_dir(cfg)
    if mem_dir is None or not mem_dir.is_dir():
        console.print("память не настроена или каталог отсутствует")
        raise typer.Exit(code=1)
    report = audit_memory(mem_dir, stale_after_days=cfg.curator.stale_after_days)
    path = _write_memory_audit(Path.cwd().resolve(), report)
    if not report.findings:
        console.print("memory curator: находок нет — память в порядке")
    else:
        for finding in report.findings:
            console.print(f"[magenta]{finding.kind}[/magenta] {finding.path}: {finding.detail}")
    console.print(f"[dim]отчёт: {path}[/dim]")


memory_proposals_app = typer.Typer(
    help="Memory proposals (блок C): ревью правок памяти, предложенных Dream.",
    no_args_is_help=True,
)
memory_app.add_typer(memory_proposals_app, name="proposals")


def _memory_dir_or_exit(cfg: SvarogConfig) -> Path:
    mem_dir = memory_dir(cfg)
    if mem_dir is None or not mem_dir.is_dir():
        console.print("память не настроена или каталог отсутствует")
        raise typer.Exit(code=1)
    return mem_dir


@memory_proposals_app.command("list")
def memory_proposals_list() -> None:
    """Показать предложения правок памяти, ожидающие ревью."""
    cfg = _load_config_or_exit()
    mem_dir = _memory_dir_or_exit(cfg)

    async def action(db: AsyncSession) -> None:
        rows = await MemoryProposalManager(db, mem_dir).list_pending()
        if not rows:
            console.print("ожидающих memory proposals нет")
            return
        for row in rows:
            console.print(
                f"[cyan]{row.id[:8]}[/cyan] {row.title} "
                f"({len(row.changes)} правок, {row.origin.value})"
            )
        console.print("[dim]review: svarog memory proposals show <id> → approve/reject <id>[/dim]")

    asyncio.run(_with_db(cfg, action))


@memory_proposals_app.command("show")
def memory_proposals_show(
    proposal_id: Annotated[str, typer.Argument(help="id proposal'а или его префикс")],
) -> None:
    """Показать замысел, обоснование и предпросмотр каждой правки."""
    cfg = _load_config_or_exit()
    mem_dir = _memory_dir_or_exit(cfg)

    async def action(db: AsyncSession) -> None:
        manager = MemoryProposalManager(db, mem_dir)
        try:
            row = await manager.get(proposal_id)
        except MemoryProposalNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        console.print(f"[bold]{row.title}[/bold] | {row.status.value} | {row.id[:8]}")
        console.print(f"  обоснование: {row.rationale}")
        for message in MemoryProposalManager.validation_messages(row):
            console.print(f"  [yellow]{message}[/yellow]")
        if await manager.head_moved(row):
            console.print(
                "[yellow]память изменилась с момента предложения — "
                "предпросмотр ниже посчитан на текущем состоянии[/yellow]"
            )
        for path, preview in manager.preview(row):
            console.print(f"\n[bold]{path}[/bold]")
            console.print(preview)

    asyncio.run(_with_db(cfg, action))


def _decide_memory_proposal(proposal_id: str, *, approved: bool, reason: str | None) -> None:
    cfg = _load_config_or_exit()
    mem_dir = _memory_dir_or_exit(cfg)

    async def action(db: AsyncSession) -> tuple[str, int]:
        manager = MemoryProposalManager(db, mem_dir)
        row = await manager.get(proposal_id)
        ids = await manager.decide(row, approved=approved, decided_by="cli", reason=reason)
        return row.id, len(ids)

    try:
        row_id, count = asyncio.run(_with_db(cfg, action))
    except MemoryProposalNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    except MemoryProposalStateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    if approved:
        console.print(
            f"[green]proposal {row_id[:8]} одобрен[/green]: {count} заявок в очереди; "
            f"применить сейчас — svarog memory flush"
        )
    else:
        console.print(f"[yellow]proposal {row_id[:8]} отклонён[/yellow]")


@memory_proposals_app.command("approve")
def memory_proposals_approve(
    proposal_id: Annotated[str, typer.Argument(help="id proposal'а или его префикс")],
    reason: Annotated[str | None, typer.Option("--reason", help="Комментарий")] = None,
) -> None:
    """Одобрить предложение: заявки уходят в очередь единственного писателя."""
    _decide_memory_proposal(proposal_id, approved=True, reason=reason)


@memory_proposals_app.command("reject")
def memory_proposals_reject(
    proposal_id: Annotated[str, typer.Argument(help="id proposal'а или его префикс")],
    reason: Annotated[str | None, typer.Option("--reason", help="Причина отказа")] = None,
) -> None:
    """Отклонить предложение. Память не меняется."""
    _decide_memory_proposal(proposal_id, approved=False, reason=reason)


tenant_app = typer.Typer(
    help="Тенанты мультиарендного режима (ADR-0012/0014).", no_args_is_help=True
)
app.add_typer(tenant_app, name="tenant")


@tenant_app.command("create")
def tenant_create(
    tenant_id: Annotated[str, typer.Argument(help="Идентификатор тенанта")],
    role: Annotated[
        str, typer.Option("--role", help="superuser (хост) | standard (только sandbox)")
    ] = "standard",
) -> None:
    """Завести тенанта: home-дерево, git-репозитории, БД и bearer-token (ADR-0014)."""
    from svarog_harness.config.paths import registry_path
    from svarog_harness.config.schema import TenantRole
    from svarog_harness.tenant import TenantExistsError, TenantRegistry, provision_tenant

    try:
        parsed_role = TenantRole(role)
    except ValueError:
        console.print(f"[red]неизвестная роль '{role}'[/red] — ожидается superuser | standard")
        raise typer.Exit(code=1) from None
    cfg = _load_config_or_exit()
    registry = TenantRegistry(registry_path(cfg))
    try:
        result = asyncio.run(provision_tenant(cfg, registry, tenant_id, parsed_role))
    except TenantExistsError:
        console.print(f"[red]тенант '{tenant_id}' уже существует[/red]")
        raise typer.Exit(code=1) from None
    console.print(
        f"[green]тенант создан:[/green] {result.tenant_id} ({parsed_role.value})\n"
        f"[dim]home: {result.home}[/dim]\n"
        f"[bold]bearer-token (сохраните — показывается один раз):[/bold]\n{result.token}"
    )


@tenant_app.command("list")
def tenant_list() -> None:
    """Список зарегистрированных тенантов."""
    from svarog_harness.config.paths import registry_path
    from svarog_harness.tenant import TenantRegistry

    cfg = _load_config_or_exit()
    tenants = TenantRegistry(registry_path(cfg)).list_tenants()
    if not tenants:
        console.print("тенантов нет — заведите: svarog tenant create <id>")
        return
    for rec in tenants:
        console.print(
            f"[bold]{rec.tenant_id}[/bold] · {rec.role.value} · "
            f"principals: {len(rec.principals)} · {rec.created_at}"
        )


@tenant_app.command("add-principal")
def tenant_add_principal(
    tenant_id: Annotated[str, typer.Argument(help="Идентификатор тенанта")],
    principal: Annotated[str, typer.Argument(help="Principal, напр. telegram:123456789")],
) -> None:
    """Привязать principal (telegram:<id> / gateway:<token>) к тенанту."""
    from svarog_harness.config.paths import registry_path
    from svarog_harness.tenant import (
        PrincipalConflictError,
        TenantRegistry,
        TenantRegistryError,
    )

    cfg = _load_config_or_exit()
    registry = TenantRegistry(registry_path(cfg))
    try:
        registry.add_principal(tenant_id, principal)
    except PrincipalConflictError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    except TenantRegistryError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]principal привязан:[/green] {principal} → {tenant_id}")


@tenant_app.command("token")
def tenant_token(
    tenant_id: Annotated[str, typer.Argument(help="Идентификатор тенанта")],
    rotate: Annotated[
        bool, typer.Option("--rotate", help="Выпустить новый токен, отозвав прежний")
    ] = False,
) -> None:
    """Показать текущий или (с --rotate) выпустить новый gateway-token тенанта."""
    from svarog_harness.config.paths import registry_path
    from svarog_harness.tenant import (
        TenantRegistry,
        current_token,
        rotate_token,
    )

    cfg = _load_config_or_exit()
    registry = TenantRegistry(registry_path(cfg))
    if registry.get(tenant_id) is None:
        console.print(f"[red]нет тенанта '{tenant_id}'[/red]")
        raise typer.Exit(code=1)
    if rotate:
        token = rotate_token(cfg, registry, tenant_id)
        console.print(f"[green]новый bearer-token[/green] для {tenant_id}:\n{token}")
        return
    saved = current_token(cfg, tenant_id)
    if not saved:
        console.print(
            f"[yellow]у {tenant_id} нет сохранённого токена[/yellow] — "
            f"svarog tenant token {tenant_id} --rotate"
        )
        raise typer.Exit(code=1)
    console.print(f"[bold]bearer-token[/bold] {tenant_id}:\n{saved}")


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
    # В multi-tenant режиме auth всегда per-tenant (bearer из реестра), поэтому
    # единый gateway.token_ref не нужен и loopback-ограничение не применяется.
    if not cfg.tenancy.enabled:
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

        from svarog_harness.config.paths import registry_path
        from svarog_harness.gateway import GatewayService, JwtResolver, TenantHub
        from svarog_harness.gateway.api import create_app
        from svarog_harness.tenant import TenantRegistry
    except ImportError:
        console.print(
            "[red]gateway требует опциональные зависимости:[/red] "
            "uv pip install 'svarog-harness[server]'"
        )
        raise typer.Exit(code=1) from None
    if cfg.tenancy.enabled:
        registry = TenantRegistry(registry_path(cfg))
        hub = TenantHub(cfg, registry)
        jwt_ref = cfg.tenancy.jwt_secret_ref
        if jwt_ref is not None:
            jwt_secret = default_secret_store(cfg.secrets.path).get(jwt_ref)
            if not jwt_secret:
                console.print(f"[red]секрет '{jwt_ref}' (jwt_secret_ref) не найден[/red]")
                raise typer.Exit(code=1)
            api = create_app(resolver=JwtResolver(hub, jwt_secret))
            mode = f"multi-tenant (JWT) | реестр: {registry_path(cfg)}"
        else:
            api = create_app(hub=hub)
            mode = f"multi-tenant (bearer) | реестр: {registry_path(cfg)}"
    else:
        api = create_app(GatewayService(cfg, workspace), bearer_token=token)
        mode = f"single-tenant | workspace: {workspace}"
    console.print(
        f"[green]Svarog gateway[/green] http://{host}:{port} | {mode}\n"
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
    # В single-tenant allowlist — из конфига; в multi-tenant allowlist задаёт
    # реестр (principal telegram:<id>), поэтому tg.allowed_users не требуется.
    if not cfg.tenancy.enabled and not tg.allowed_users:
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

    from svarog_harness.config.paths import registry_path
    from svarog_harness.gateway import GatewayService, TenantHub
    from svarog_harness.gateway.telegram import (
        HttpxTelegramTransport,
        TelegramBot,
    )
    from svarog_harness.tenant import TenantRegistry

    transport = HttpxTelegramTransport(token)
    supervised = cfg.supervisor.auto_resume_refuel
    if cfg.tenancy.enabled:
        registry = TenantRegistry(registry_path(cfg))
        hub = TenantHub(cfg, registry)
        bot = TelegramBot.from_hub(hub, registry, transport, poll_timeout=tg.poll_timeout_sec)
        supervisor = hub.run_supervisor if supervised else None
        mode = f"multi-tenant | реестр: {registry_path(cfg)}"
    else:
        service = GatewayService(cfg, workspace)
        bot = TelegramBot(
            service,
            transport,
            allowed_users=set(tg.allowed_users),
            poll_timeout=tg.poll_timeout_sec,
        )
        supervisor = service.run_supervisor if supervised else None
        mode = f"single-tenant | allowlist: {len(tg.allowed_users)} user(s)"
    console.print(
        f"[green]Svarog Telegram bot[/green] | workspace: {workspace} | {mode}"
        + (" | refuel-supervisor on" if supervised else "")
        + "\n[dim]Ctrl-C для остановки[/dim]"
    )

    async def run_all() -> None:
        # Бот и супервизор refuel (§6.10) живут параллельно в одном процессе.
        tasks = [asyncio.ensure_future(bot.run_forever())]
        if supervisor is not None:
            tasks.append(asyncio.ensure_future(supervisor()))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_all())


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
