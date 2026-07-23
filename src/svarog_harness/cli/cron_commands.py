"""Команды `svarog cron`: джобы планировщика (ADR-0019).

Вынесено из main.py: группа самодостаточна — общие хелперы берутся из
cli/_shared.py, поэтому модуль не импортирует main.py и цикла не создаёт.
"""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.cli._shared import console, load_config_or_exit, resolve_autonomy
from svarog_harness.cli.chat_engine import with_db
from svarog_harness.runtime.config_snapshot import config_digest
from svarog_harness.scheduler.schedule import ScheduleSpecError, next_run_after, parse_spec
from svarog_harness.scheduler.store import JobNotFoundError, JobStore, ProtectedJobError
from svarog_harness.storage.models import CronJob, JobOrigin, ScheduleKind, utcnow

cron_app = typer.Typer(help="Джобы планировщика (ADR-0019).", no_args_is_help=True)


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
    cfg = load_config_or_exit(project_dir=workspace)
    autonomy = resolve_autonomy(cfg, yolo=yolo, auto=auto, supervised=supervised)
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

    asyncio.run(with_db(cfg, action))


@cron_app.command("list")
def cron_list(
    json_output: Annotated[
        bool, typer.Option("--json", help="NDJSON: по одному объекту на строку")
    ] = False,
) -> None:
    """Показать джобы планировщика."""
    cfg = load_config_or_exit()

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

    asyncio.run(with_db(cfg, action))


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
    cfg = load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        try:
            job = await _resolve_job(db, job_id)
        except JobNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        await JobStore(db).set_enabled(job, enabled)
        state = "включена" if enabled else "выключена"
        console.print(f"джоба [bold]{job.name}[/bold] {state}")

    asyncio.run(with_db(cfg, action))


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
    cfg = load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        try:
            job = await _resolve_job(db, job_id)
        except JobNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        print(json.dumps(_job_row(job) | {"task": job.task}, ensure_ascii=False, indent=2))

    asyncio.run(with_db(cfg, action))


@cron_app.command("remove")
def cron_remove(job_id: Annotated[str, typer.Argument(help="id джобы или префикс")]) -> None:
    """Удалить джобу. Системные джобы удалить нельзя."""
    cfg = load_config_or_exit()

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

    asyncio.run(with_db(cfg, action))
