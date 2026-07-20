"""Просмотр traces для CLI (§6.12): выборка из storage и Rich-рендеринг."""

from dataclasses import dataclass
from typing import Any

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.storage.models import CheckResult, Message, Run, RunState, Session, ToolCall
from svarog_harness.trace.lookup import RunNotFoundError, find_run_by_prefix

# Незавершённые состояния run'а — «занимают слот» для квоты одновременных (ADR-0014).
_ACTIVE_RUN_STATES = (
    RunState.PENDING,
    RunState.RUNNING,
    RunState.WAITING_APPROVAL,
    RunState.SUSPENDED,
)


async def run_usage_totals(db: AsyncSession) -> tuple[int, float, int]:
    """(активные run'ы, суммарная стоимость USD, суммарные токены) по всей БД тенанта."""
    active = await db.scalar(
        select(func.count()).select_from(Run).where(Run.state.in_(_ACTIVE_RUN_STATES))
    )
    cost = await db.scalar(select(func.coalesce(func.sum(Run.cost_usd), 0.0)))
    tokens = await db.scalar(select(func.coalesce(func.sum(Run.tokens_used), 0)))
    return int(active or 0), float(cost or 0.0), int(tokens or 0)


@dataclass(frozen=True)
class SessionSummary:
    """Строка `sessions list`: сессия + агрегаты по её runs."""

    session: Session
    runs: int
    last_task: str


async def fetch_sessions(
    db: AsyncSession, *, limit: int = 20, search: str | None = None
) -> list[SessionSummary]:
    """Сессии от свежих к старым; search — подстрока в title или задачах runs."""
    last_run = (
        select(Run.task)
        .where(Run.session_id == Session.id)
        .order_by(Run.created_at.desc())
        .limit(1)
        .correlate(Session)
        .scalar_subquery()
    )
    stmt = (
        select(Session, func.count(Run.id), func.coalesce(last_run, ""))
        .join(Run, Run.session_id == Session.id)
        .group_by(Session.id)
        .order_by(Session.updated_at.desc())
    )
    if search:
        needle = f"%{search}%"
        matching_runs = select(Run.session_id).where(Run.task.like(needle)).scalar_subquery()
        stmt = stmt.where(Session.title.like(needle) | Session.id.in_(matching_runs))
    stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).all()
    return [SessionSummary(session=s, runs=int(n), last_task=str(t)) for s, n, t in rows]


def session_to_dict(summary: SessionSummary) -> dict[str, Any]:
    return {
        "id": summary.session.id,
        "title": summary.session.title,
        "runs": summary.runs,
        "last_task": summary.last_task,
        "created_at": _iso(summary.session.created_at),
        "updated_at": _iso(summary.session.updated_at),
    }


def render_sessions_table(summaries: list[SessionSummary]) -> Table:
    table = Table(title="sessions")
    table.add_column("id", style="cyan")
    table.add_column("title")
    table.add_column("runs", justify="right")
    table.add_column("последняя задача")
    table.add_column("обновлена")
    for s in summaries:
        table.add_row(
            s.session.id[:8],
            s.session.title or "—",
            str(s.runs),
            s.last_task[:60],
            s.session.updated_at.strftime("%Y-%m-%d %H:%M"),
        )
    return table


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def run_to_dict(run: Run) -> dict[str, Any]:
    """Плоское JSON-представление run'а (NDJSON-строка `traces list --json`)."""
    return {
        "id": run.id,
        "session_id": run.session_id,
        "parent_run_id": run.parent_run_id,
        "state": run.state.value,
        "task": run.task,
        "autonomy": run.autonomy,
        "iterations": run.iterations,
        "tokens_used": run.tokens_used,
        "cost_usd": run.cost_usd,
        "error": run.error,
        "workspace": run.workspace,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "meta": run.meta,
    }


def run_detail_to_dict(
    run: Run, messages: list[Message], tool_calls: list[ToolCall], checks: list[CheckResult]
) -> dict[str, Any]:
    """Полный trace одного run (`traces show --json`)."""
    return {
        "run": run_to_dict(run),
        "messages": [
            {"index": m.index_in_run, "role": m.role, "content": m.content} for m in messages
        ],
        "tool_calls": [
            {
                "id": tc.id,
                "tool_name": tc.tool_name,
                "arguments": tc.arguments,
                "risk_level": tc.risk_level,
                "policy_decision": tc.policy_decision,
                "status": tc.status.value,
                "result": tc.result,
                "error": tc.error,
                "started_at": _iso(tc.started_at),
                "finished_at": _iso(tc.finished_at),
            }
            for tc in tool_calls
        ],
        "checks": [
            {"name": c.check_name, "status": c.status.value, "output": c.output} for c in checks
        ],
    }


__all__ = [
    "RunNotFoundError",
    "fetch_run",
    "fetch_runs",
    "render_run",
    "render_runs_table",
    "run_detail_to_dict",
    "run_to_dict",
    "run_usage_totals",
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


async def fetch_run(
    db: AsyncSession, run_id: str
) -> tuple[Run, list[Message], list[ToolCall], list[CheckResult]]:
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
    checks = list(
        (
            await db.execute(
                select(CheckResult)
                .where(CheckResult.run_id == run.id)
                .order_by(CheckResult.created_at)
            )
        ).scalars()
    )
    return run, messages, tool_calls, checks


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


def render_run(
    run: Run,
    messages: list[Message],
    tool_calls: list[ToolCall],
    checks: list[CheckResult] | None = None,
) -> RenderableType:
    header = Table.grid(padding=(0, 2))
    header.add_column(style="bold")
    header.add_column()
    header.add_row("run", run.id)
    header.add_row("состояние", _state_text(run.state.value))
    header.add_row("задача", run.task)
    header.add_row("автономия", run.autonomy)
    header.add_row("модель", str((run.meta or {}).get("model", "?")))
    cached = int((run.meta or {}).get("cached_tokens", 0))
    cached_suffix = f", из них {cached} из кэша" if cached else ""
    header.add_row(
        "итог",
        f"{run.iterations} итераций, {run.tokens_used} токенов{cached_suffix}, ${run.cost_usd:.4f}",
    )
    phases = (run.meta or {}).get("phases") or {}
    if phases:
        parts = [
            f"{name} {entry['ms']}мс×{entry['count']}"
            for name, entry in sorted(phases.items())
            if isinstance(entry, dict)
        ]
        header.add_row("фазы", ", ".join(parts) + f" | последняя: {phases.get('last', '?')}")
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
        calls_table.add_column("policy")
        calls_table.add_column("ошибка", max_width=60)
        for call in tool_calls:
            calls_table.add_row(
                call.tool_name,
                call.status.value,
                call.risk_level or "-",
                _policy_text(call.policy_decision),
                call.error or "",
            )
        blocks.append(calls_table)

    if checks:
        checks_table = Table(title="Checks (verifier)")
        checks_table.add_column("проверка", style="cyan")
        checks_table.add_column("статус")
        for check in checks:
            style = "green" if check.status.value == "passed" else "red"
            checks_table.add_row(check.check_name, Text(check.status.value, style=style))
        blocks.append(checks_table)
    return Group(*blocks)


# notify и deny выделяются в trace (ADR-0010: post-hoc review вместо pre-approval).
_POLICY_STYLES = {
    "notify": "bold yellow",
    "deny": "bold red",
    "require_approval": "bold magenta",
}


def _policy_text(decision: str | None) -> Text:
    if not decision:
        return Text("-")
    return Text(decision, style=_POLICY_STYLES.get(decision, "white"))
