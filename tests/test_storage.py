from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import (
    Approval,
    Checkpoint,
    MemoryChange,
    MemoryChangeStatus,
    Message,
    Run,
    RunState,
    Session,
    ToolCall,
    ToolCallStatus,
)

EXPECTED_TABLES = {
    "sessions",
    "runs",
    "messages",
    "tool_calls",
    "approvals",
    "checkpoints",
    "memory_queue",
    "skill_loads",
    "check_results",
    "artifacts",
    "error_events",
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "svarog.sqlite3"
    init_db(path)
    return path


@pytest.fixture
async def engine(db_path: Path):
    engine = create_engine(db_path)
    yield engine
    await engine.dispose()


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names()))


async def test_init_db_creates_all_tables(db_path: Path, engine: AsyncEngine) -> None:
    assert db_path.is_file()
    tables = await _table_names(engine)
    assert tables >= EXPECTED_TABLES
    assert "alembic_version" in tables


async def test_init_db_is_idempotent(db_path: Path, engine: AsyncEngine) -> None:
    init_db(db_path)
    assert await _table_names(engine) >= EXPECTED_TABLES


async def test_run_lifecycle_roundtrip(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        session = Session(title="test session")
        run = Run(session=session, task="сделай хорошо", autonomy="yolo")
        run.messages.append(Message(index_in_run=0, role="user", content={"text": "привет"}))
        run.tool_calls.append(
            ToolCall(
                tool_name="read_file",
                arguments={"path": "README.md"},
                status=ToolCallStatus.SUCCEEDED,
                policy_decision="allow",
            )
        )
        run.checkpoints.append(Checkpoint(iteration=1, state={"step": "done"}))
        db.add(session)
        await db.commit()
        run_id = run.id

    async with factory() as db:
        loaded = await db.get(Run, run_id)
        assert loaded is not None
        assert loaded.state is RunState.PENDING
        assert loaded.autonomy == "yolo"
        loaded.state = RunState.COMPLETED
        await db.commit()

    async with factory() as db:
        completed = (
            await db.execute(select(Run).where(Run.state == RunState.COMPLETED))
        ).scalar_one()
        assert completed.id == run_id


async def test_cascade_delete_session_removes_runs(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        session = Session()
        run = Run(session=session, task="t", autonomy="yolo")
        run.messages.append(Message(index_in_run=0, role="user", content={}))
        db.add(session)
        await db.commit()
        session_id = session.id

    async with factory() as db:
        await db.delete(await db.get(Session, session_id))
        await db.commit()

    async with factory() as db:
        assert (await db.execute(select(Run))).scalars().all() == []
        assert (await db.execute(select(Message))).scalars().all() == []


async def test_foreign_keys_enforced(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add(Run(session_id="nonexistent", task="t", autonomy="yolo"))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_message_index_unique_per_run(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        run = Run(session=Session(), task="t", autonomy="yolo")
        run.messages.append(Message(index_in_run=0, role="user", content={}))
        run.messages.append(Message(index_in_run=0, role="assistant", content={}))
        db.add(run)
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_memory_queue_and_approvals_defaults(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        run = Run(session=Session(), task="t", autonomy="yolo")
        db.add(run)
        await db.flush()  # присвоить run.id до использования в FK
        db.add(MemoryChange(change={"op": "append", "file": "notes.md"}, source_run_id=run.id))
        db.add(Approval(run_id=run.id, action_type="git.push", payload={"branch": "x"}))
        await db.commit()

    async with factory() as db:
        pending = (
            await db.execute(
                select(MemoryChange).where(MemoryChange.status == MemoryChangeStatus.PENDING)
            )
        ).scalar_one()
        assert pending.change["op"] == "append"
        approval = (await db.execute(select(Approval))).scalar_one()
        assert approval.status.value == "pending"
