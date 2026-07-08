"""Просмотр traces для CLI (§6.12): выборка из storage и Rich-рендеринг."""

from typing import Any

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.storage.models import Message, Run, ToolCall
from svarog_harness.trace.lookup import RunNotFoundError, find_run_by_prefix

__all__ = [
    "RunNotFoundError",
    "fetch_run",
    "fetch_runs",
    "render_run",
    "render_runs_table",
]

_STATE_STYLES = {
    "completed": "green",
    "failed": "red",
    "running": "yellow",
    "suspended": "yellow",
    "waiting_approval": "magenta",
}


async def fetch_runs(db: AsyncSession, limit: int = 20) -> list[Run]:
    result = await db.execute(select(Run).order_by(Run.created_at.desc()).limit(limit))
    return list(result.scalars())


async def fetch_run(db: AsyncSession, run_id: str) -> tuple[Run, list[Message], list[ToolCall]]:
    """Найти run по полному id или уникальному префиксу."""
    run = await find_run_by_prefix(db, run_id)
    messages = list(
        (
            await db.execute(
                select(Message).where(Message.run_id == run.id).order_by(Message.index_in_run)
            )
        ).scalars()
    )
    tool_calls = list(
        (
            await db.execute(
                select(ToolCall).where(ToolCall.run_id == run.id).order_by(ToolCall.created_at)
            )
        ).scalars()
    )
    return run, messages, tool_calls


def _state_text(state: str) -> Text:
    return Text(state, style=_STATE_STYLES.get(state, "white"))


def render_runs_table(runs: list[Run]) -> Table:
    table = Table(title="Runs", show_lines=False)
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("состояние")
    table.add_column("задача", max_width=48)
    table.add_column("итер.", justify="right")
    table.add_column("токены", justify="right")
    table.add_column("$", justify="right")
    table.add_column("начат", no_wrap=True)
    for run in runs:
        table.add_row(
            run.id[:8],
            _state_text(run.state.value),
            run.task,
            str(run.iterations),
            str(run.tokens_used),
            f"{run.cost_usd:.4f}",
            run.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
    return table


def _message_body(content: dict[str, Any]) -> str:
    parts: list[str] = []
    if content.get("content"):
        parts.append(str(content["content"]))
    for call in content.get("tool_calls", []):
        parts.append(f"→ {call['name']}({call['arguments']})")
    return "\n".join(parts) or "(пусто)"


def render_run(run: Run, messages: list[Message], tool_calls: list[ToolCall]) -> RenderableType:
    header = Table.grid(padding=(0, 2))
    header.add_column(style="bold")
    header.add_column()
    header.add_row("run", run.id)
    header.add_row("состояние", _state_text(run.state.value))
    header.add_row("задача", run.task)
    header.add_row("автономия", run.autonomy)
    header.add_row("модель", str((run.meta or {}).get("model", "?")))
    header.add_row(
        "итог",
        f"{run.iterations} итераций, {run.tokens_used} токенов, ${run.cost_usd:.4f}",
    )
    if run.error:
        header.add_row("ошибка", Text(run.error, style="red"))

    blocks: list[RenderableType] = [Panel(header, title="Trace")]
    for message in messages:
        style = {"system": "dim", "user": "blue", "assistant": "green", "tool": "yellow"}.get(
            message.role, "white"
        )
        blocks.append(
            Panel(
                _message_body(message.content),
                title=f"[{message.index_in_run}] {message.role}",
                border_style=style,
            )
        )

    if tool_calls:
        calls_table = Table(title="Tool calls")
        calls_table.add_column("tool", style="cyan")
        calls_table.add_column("статус")
        calls_table.add_column("риск")
        calls_table.add_column("ошибка", max_width=60)
        for call in tool_calls:
            calls_table.add_row(
                call.tool_name,
                call.status.value,
                call.risk_level or "-",
                call.error or "",
            )
        blocks.append(calls_table)
    return Group(*blocks)
