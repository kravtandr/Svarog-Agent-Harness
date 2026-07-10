"""Тесты LockBackend (ADR-0007): межпроцессная сериализация memory writer."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.writer import MemoryWriter
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.locks import FileLockBackend, default_lock_backend
from svarog_harness.storage.models import MemoryChangeStatus
from svarog_harness.trace.recorder import TraceRecorder


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _memory_repo(tmp_path: Path) -> Path:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    repo = GitRepo(memory_dir)
    await repo.init()
    await repo.ensure_identity()
    return memory_dir


# --- FileLockBackend: базовая семантика ---


async def test_lock_acquired_then_released(tmp_path: Path) -> None:
    backend = FileLockBackend(tmp_path / "locks")
    async with backend.guard("k") as acquired:
        assert acquired is True
    # После выхода лок свободен — можно взять снова.
    async with backend.guard("k") as again:
        assert again is True


async def test_second_guard_fails_while_held(tmp_path: Path) -> None:
    backend = FileLockBackend(tmp_path / "locks")
    async with backend.guard("k") as first:
        assert first is True
        # timeout=0 → одна попытка, лок занят → False, без ожидания.
        async with backend.guard("k", timeout=0.0) as second:
            assert second is False


async def test_different_keys_do_not_block(tmp_path: Path) -> None:
    backend = FileLockBackend(tmp_path / "locks")
    async with backend.guard("a") as a, backend.guard("b") as b:
        assert a is True
        assert b is True


async def test_guard_released_on_exception(tmp_path: Path) -> None:
    backend = FileLockBackend(tmp_path / "locks")
    with pytest.raises(RuntimeError):
        async with backend.guard("k") as acquired:
            assert acquired is True
            raise RuntimeError("boom")
    # Лок должен освободиться, несмотря на исключение.
    async with backend.guard("k", timeout=0.0) as after:
        assert after is True


# --- MemoryWriter под локом ---


async def test_writer_drains_normally_with_lock(db: AsyncSession, tmp_path: Path) -> None:
    memory_dir = await _memory_repo(tmp_path)
    lock = default_lock_backend(tmp_path / "state" / "svarog.db")
    writer = MemoryWriter(db, memory_dir, lock=lock)
    await writer.enqueue(
        MemoryChangeRequest(file="a.md", operation=MemoryOperation.CREATE, content="x\n")
    )
    processed = await writer.drain()
    assert len(processed) == 1
    assert processed[0].status is MemoryChangeStatus.APPLIED


async def test_writer_skips_when_lock_held(db: AsyncSession, tmp_path: Path) -> None:
    memory_dir = await _memory_repo(tmp_path)
    backend = FileLockBackend(tmp_path / "locks")
    writer = MemoryWriter(db, memory_dir, lock=backend)
    run = await TraceRecorder(db).start_run(task="t", autonomy="yolo", model="test")
    await writer.enqueue(
        MemoryChangeRequest(
            file="a.md", operation=MemoryOperation.CREATE, content="x\n", source_run_id=run.id
        )
    )
    # Держим тот же ключ, что использует writer, — drain должен уступить.
    key = f"memory-writer:{memory_dir.resolve()}"
    async with backend.guard(key) as held:
        assert held is True
        processed = await writer.drain()
        assert processed == []
    # Заявка осталась PENDING — следующий drain её применит.
    processed = await writer.drain()
    assert len(processed) == 1
    assert processed[0].status is MemoryChangeStatus.APPLIED
