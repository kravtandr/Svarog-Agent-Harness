"""Команды `svarog approvals`: список approval-запросов и решения по ним."""

import asyncio
from typing import Annotated

import typer
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.cli._shared import console, load_config_or_exit, show_approval
from svarog_harness.cli.chat_engine import with_db
from svarog_harness.storage.models import Approval
from svarog_harness.trace.lookup import ApprovalNotFoundError
from svarog_harness.trace.recorder import TraceRecorder

approvals_app = typer.Typer(help="Approval-запросы: список и решения.", no_args_is_help=True)


@approvals_app.command("list")
def approvals_list() -> None:
    """Показать ожидающие approval-запросы."""
    cfg = load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        approvals = await TraceRecorder(db).fetch_pending_approvals()
        if not approvals:
            console.print("ожидающих approvals нет")
            return
        for approval in approvals:
            show_approval(approval)
            console.print(f"  [dim]решение: svarog approvals approve/deny {approval.id[:8]}[/dim]")

    asyncio.run(with_db(cfg, action))


def _decide_approval_command(approval_id: str, *, approved: bool, reason: str | None) -> None:
    cfg = load_config_or_exit()

    async def action(db: AsyncSession) -> Approval:
        recorder = TraceRecorder(db)
        approval = await recorder.find_approval_by_prefix(approval_id)
        await recorder.decide_approval(approval, approved=approved, decided_by="cli", reason=reason)
        return approval

    try:
        approval = asyncio.run(with_db(cfg, action))
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
    cfg = load_config_or_exit()

    async def action(db: AsyncSession) -> Approval:
        recorder = TraceRecorder(db)
        approval = await recorder.find_approval_by_prefix(approval_id)
        await recorder.answer_question(approval, answer=text, answered_by="cli")
        return approval

    try:
        approval = asyncio.run(with_db(cfg, action))
    except ApprovalNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"вопрос {approval.id[:8]} [green]отвечен[/green]")
    console.print(f"[dim]продолжить run: svarog resume {approval.run_id[:8]}[/dim]")
