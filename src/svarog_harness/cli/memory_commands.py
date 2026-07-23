"""Команды `svarog memory`: показ, слив очереди, курирование, proposals (Flow A).

Вынесено из main.py: общие хелперы берутся из cli/_shared.py, поэтому модуль
не импортирует main.py и цикла не создаёт.
"""

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.cli._shared import console, known_secret_values, load_config_or_exit
from svarog_harness.cli.chat_engine import with_db
from svarog_harness.config.paths import memory_dir
from svarog_harness.config.schema import SvarogConfig
from svarog_harness.memory import MemoryWriter, read_memory
from svarog_harness.memory.curator import MemoryAuditReport, audit_memory
from svarog_harness.memory.proposal_manager import (
    MemoryProposalManager,
    MemoryProposalNotFoundError,
    MemoryProposalStateError,
)
from svarog_harness.secrets import default_secret_store
from svarog_harness.storage.locks import default_lock_backend

memory_app = typer.Typer(help="Память агента (Flow A).", no_args_is_help=True)


@memory_app.command("show")
def memory_show() -> None:
    """Показать память, как она попадёт в контекст."""
    cfg = load_config_or_exit()
    mem_dir = memory_dir(cfg)
    if mem_dir is None:
        console.print("память не настроена (задайте memory.path в svarog.yaml)")
        return
    text = read_memory(mem_dir, limit_bytes=cfg.memory.context_limit_bytes)
    console.print(text or "память пуста")


@memory_app.command("flush")
def memory_flush() -> None:
    """Применить очередь заявок памяти single writer'ом (обычно вызывается после run)."""
    cfg = load_config_or_exit()
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
        rows = await writer.drain(known_values=known_secret_values(cfg, store))
        for row in rows:
            if row.error:
                console.print(f"[yellow]отклонено: {row.error}[/yellow]")
            elif row.commit_sha:
                console.print(f"[green]{row.commit_sha}[/green] применено")
        return len(rows)

    count = asyncio.run(with_db(cfg, action))
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
    cfg = load_config_or_exit()
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
    cfg = load_config_or_exit()
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

    asyncio.run(with_db(cfg, action))


@memory_proposals_app.command("show")
def memory_proposals_show(
    proposal_id: Annotated[str, typer.Argument(help="id proposal'а или его префикс")],
) -> None:
    """Показать замысел, обоснование и предпросмотр каждой правки."""
    cfg = load_config_or_exit()
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

    asyncio.run(with_db(cfg, action))


def _decide_memory_proposal(proposal_id: str, *, approved: bool, reason: str | None) -> None:
    cfg = load_config_or_exit()
    mem_dir = _memory_dir_or_exit(cfg)

    async def action(db: AsyncSession) -> tuple[str, int]:
        manager = MemoryProposalManager(db, mem_dir)
        row = await manager.get(proposal_id)
        ids = await manager.decide(row, approved=approved, decided_by="cli", reason=reason)
        return row.id, len(ids)

    try:
        row_id, count = asyncio.run(with_db(cfg, action))
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
