"""Команды `svarog traces` и `svarog sessions`: просмотр аудита runs (§6.12, §15).

Вынесено из main.py: общие хелперы берутся из cli/_shared.py, поэтому модуль
не импортирует main.py и цикла не создаёт.
"""

import asyncio
import json
from typing import Annotated

import typer
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.cli._shared import console, load_config_or_exit
from svarog_harness.cli.chat_engine import with_db
from svarog_harness.trace.lookup import (
    RunNotFoundError,
    SessionNotFoundError,
    find_session_by_prefix,
)
from svarog_harness.trace.recorder import TraceRecorder
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

traces_app = typer.Typer(help="Просмотр traces выполненных runs.", no_args_is_help=True)
sessions_app = typer.Typer(
    help="Сессии: список, поиск, переименование (продолжение — chat --session).",
    no_args_is_help=True,
)


@traces_app.command("list")
def traces_list(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Сколько runs показать")] = 20,
    json_output: Annotated[
        bool, typer.Option("--json", help="NDJSON: по одному JSON-объекту на строку")
    ] = False,
) -> None:
    """Показать последние runs."""
    cfg = load_config_or_exit()

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

    asyncio.run(with_db(cfg, action))


@traces_app.command("show")
def traces_show(
    run_id: Annotated[str, typer.Argument(help="id run'а или его уникальный префикс")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Полный trace одним JSON-объектом")
    ] = False,
) -> None:
    """Показать полный trace одного run."""
    cfg = load_config_or_exit()

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

    asyncio.run(with_db(cfg, action))


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
    cfg = load_config_or_exit()

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

    asyncio.run(with_db(cfg, action))


@sessions_app.command("rename")
def sessions_rename(
    session_id: Annotated[str, typer.Argument(help="id сессии или её префикс")],
    title: Annotated[str, typer.Argument(help="Новое название")],
) -> None:
    """Переименовать сессию."""
    cfg = load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        session = await find_session_by_prefix(db, session_id)
        await TraceRecorder(db).rename_session(session, title)
        console.print(f"[green]сессия {session.id[:8]} → «{title}»[/green]")

    try:
        asyncio.run(with_db(cfg, action))
    except SessionNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
