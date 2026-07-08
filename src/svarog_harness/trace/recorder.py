"""Запись trace в storage (§6.12, §15): сообщения, tool calls, прогресс run'а.

Recorder — единственное место, где agent loop пишет в БД; сам loop
не знает про SQLAlchemy-модели напрямую.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.storage.models import (
    Message,
    Run,
    RunState,
    Session,
    ToolCall,
    ToolCallStatus,
    utcnow,
)


class TraceRecorder:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._message_index: dict[str, int] = {}

    async def start_run(self, *, task: str, autonomy: str, model: str) -> Run:
        session = Session(title=task[:200])
        run = Run(
            session=session,
            state=RunState.RUNNING,
            task=task,
            autonomy=autonomy,
            started_at=utcnow(),
            meta={"model": model},
        )
        self._db.add(session)
        self._db.add(run)
        await self._db.flush()
        self._message_index[run.id] = 0
        await self._db.commit()
        return run

    async def add_message(self, run: Run, role: str, content: dict[str, Any]) -> Message:
        index = self._message_index.get(run.id, 0)
        self._message_index[run.id] = index + 1
        message = Message(run_id=run.id, index_in_run=index, role=role, content=content)
        self._db.add(message)
        await self._db.commit()
        return message

    async def start_tool_call(
        self, run: Run, *, tool_name: str, arguments: dict[str, Any], risk_level: str | None
    ) -> ToolCall:
        tool_call = ToolCall(
            run_id=run.id,
            tool_name=tool_name,
            arguments=arguments,
            risk_level=risk_level,
            status=ToolCallStatus.RUNNING,
            started_at=utcnow(),
        )
        self._db.add(tool_call)
        await self._db.commit()
        return tool_call

    async def finish_tool_call(
        self, tool_call: ToolCall, *, ok: bool, output: str, error: str | None
    ) -> None:
        tool_call.status = ToolCallStatus.SUCCEEDED if ok else ToolCallStatus.FAILED
        tool_call.result = {"output": output}
        tool_call.error = error
        tool_call.finished_at = utcnow()
        await self._db.commit()

    async def update_progress(
        self, run: Run, *, iterations: int, tokens_used: int, cost_usd: float
    ) -> None:
        run.iterations = iterations
        run.tokens_used = tokens_used
        run.cost_usd = cost_usd
        await self._db.commit()

    async def finish_run(self, run: Run, state: RunState, *, error: str | None = None) -> None:
        run.state = state
        run.error = error
        run.finished_at = utcnow()
        await self._db.commit()
