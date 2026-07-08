"""Точка входа CLI `svarog` (§10.1).

Команды init/chat/skills/approvals добавляются по мере milestones
(см. docs/first-issues.md); в M1 доступны run, traces list/show, version.
"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness import __version__
from svarog_harness.config.loader import ConfigError, load_config
from svarog_harness.config.schema import AutonomyMode, SvarogConfig
from svarog_harness.llm.openai_compatible import ApiKeyError, default_provider
from svarog_harness.runtime.loop import AgentLoop, RunOutcome
from svarog_harness.sandbox import ExecutionEnvironment, SandboxError, create_environment
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import RunState
from svarog_harness.tools.file_tools import file_tools
from svarog_harness.tools.registry import ToolRegistry
from svarog_harness.tools.shell import BashTool
from svarog_harness.trace.recorder import TraceRecorder
from svarog_harness.trace.viewer import (
    RunNotFoundError,
    fetch_run,
    fetch_runs,
    render_run,
    render_runs_table,
)

app = typer.Typer(
    name="svarog",
    help="Svarog — Git-native runtime for self-hosted AI agents.",
    no_args_is_help=True,
)
traces_app = typer.Typer(help="Просмотр traces выполненных runs.", no_args_is_help=True)
app.add_typer(traces_app, name="traces")
console = Console()


@app.callback()
def main() -> None:
    """Svarog CLI."""


@app.command()
def version() -> None:
    """Показать версию svarog-harness."""
    console.print(f"svarog-harness {__version__}")


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
    workspace: Path, environment: ExecutionEnvironment, command_timeout_sec: float
) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in file_tools(workspace):
        registry.register(tool)
    registry.register(BashTool(environment, command_timeout_sec))
    return registry


def _first_existing_skills_dir(cfg: SvarogConfig, workspace: Path) -> Path | None:
    """Первый существующий каталог skills — mount ro в sandbox (ADR-0002)."""
    for raw in cfg.skills.paths:
        path = raw.expanduser()
        if not path.is_absolute():
            path = workspace / path
        if path.is_dir():
            return path.resolve()
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


async def _run_task(
    cfg: SvarogConfig, workspace: Path, task: str, autonomy: AutonomyMode
) -> RunOutcome:
    environment = create_environment(
        cfg.sandbox, workspace, skills_dir=_first_existing_skills_dir(cfg, workspace)
    )
    await environment.start()
    try:

        async def action(db: AsyncSession) -> RunOutcome:
            loop = AgentLoop(
                default_provider(cfg.models),
                _build_registry(workspace, environment, cfg.sandbox.timeout_sec),
                TraceRecorder(db),
                cfg.runtime,
                workspace,
                model_name=cfg.models.providers[cfg.models.default].model,
                on_text_delta=lambda delta: console.print(delta, end="", highlight=False),
                on_tool_call=lambda name, args: console.print(
                    f"\n[dim]→ {name} {args}[/dim]", highlight=False
                ),
            )
            return await loop.run(task, autonomy)

        return await _with_db(cfg, action)
    finally:
        await environment.cleanup()


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

    console.print()
    stats = (
        f"run {outcome.run_id[:8]} | {outcome.iterations} итераций | "
        f"{outcome.tokens_used} токенов | ${outcome.cost_usd:.4f}"
    )
    if outcome.state is RunState.COMPLETED:
        console.print(f"[green]completed[/green] | {stats}")
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
            run, messages, tool_calls = await fetch_run(db, run_id)
        except RunNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        console.print(render_run(run, messages, tool_calls))

    asyncio.run(_with_db(cfg, action))
