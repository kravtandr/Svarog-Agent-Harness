"""Тесты памяти Flow A (§6.7, ADR-0004): apply, reader, single writer, git-коммиты."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.memory.apply import MemoryApplyError, apply_change
from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.reader import read_memory
from svarog_harness.memory.writer import MemoryWriter
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import MemoryChange, MemoryChangeStatus
from svarog_harness.trace.recorder import TraceRecorder


async def _make_run(db: AsyncSession, task: str) -> str:
    """Создать реальный Run (source_run_id — FK на runs.id)."""
    run = await TraceRecorder(db).start_run(task=task, autonomy="yolo", model="test")
    return run.id


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


# --- apply_change ---


def test_apply_create_and_append(tmp_path: Path) -> None:
    apply_change(
        tmp_path,
        MemoryChangeRequest(
            file="user/profile.md", operation=MemoryOperation.CREATE, content="# Профиль\n"
        ),
    )
    assert (tmp_path / "user/profile.md").read_text(encoding="utf-8") == "# Профиль\n"

    apply_change(
        tmp_path,
        MemoryChangeRequest(
            file="user/profile.md", operation=MemoryOperation.APPEND, content="любит краткость\n"
        ),
    )
    text = (tmp_path / "user/profile.md").read_text(encoding="utf-8")
    assert "любит краткость" in text


def test_apply_replace_section(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text(
        "# Заметки\n\n## Проект\nстарое\n\n## Прочее\nхвост\n", encoding="utf-8"
    )
    apply_change(
        tmp_path,
        MemoryChangeRequest(
            file="notes.md",
            operation=MemoryOperation.REPLACE_SECTION,
            section="Проект",
            content="новое содержимое",
        ),
    )
    text = (tmp_path / "notes.md").read_text(encoding="utf-8")
    assert "новое содержимое" in text
    assert "старое" not in text
    assert "## Прочее" in text  # соседняя секция цела
    assert "хвост" in text


def test_apply_replace_missing_section(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("# Заметки\n", encoding="utf-8")
    with pytest.raises(MemoryApplyError, match="секция"):
        apply_change(
            tmp_path,
            MemoryChangeRequest(
                file="notes.md",
                operation=MemoryOperation.REPLACE_SECTION,
                section="Нет",
                content="x",
            ),
        )


def test_apply_delete(tmp_path: Path) -> None:
    (tmp_path / "old.md").write_text("удалить", encoding="utf-8")
    apply_change(tmp_path, MemoryChangeRequest(file="old.md", operation=MemoryOperation.DELETE))
    assert not (tmp_path / "old.md").exists()


def test_apply_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(MemoryApplyError, match="пределы"):
        apply_change(
            tmp_path,
            MemoryChangeRequest(file="../escape.md", operation=MemoryOperation.CREATE, content="x"),
        )


# --- reader ---


def test_read_memory_concatenates(tmp_path: Path) -> None:
    (tmp_path / "user").mkdir()
    (tmp_path / "user/profile.md").write_text("любит Python", encoding="utf-8")
    (tmp_path / "projects.md").write_text("проект Svarog", encoding="utf-8")
    text = read_memory(tmp_path)
    assert "любит Python" in text
    assert "проект Svarog" in text
    assert "user/profile.md" in text


def test_read_memory_respects_limit(tmp_path: Path) -> None:
    (tmp_path / "big.md").write_text("x" * 5000, encoding="utf-8")
    (tmp_path / "more.md").write_text("y" * 5000, encoding="utf-8")
    text = read_memory(tmp_path, limit_bytes=3000)
    assert "усечена" in text


def test_read_memory_missing_dir(tmp_path: Path) -> None:
    assert read_memory(tmp_path / "nope") == ""


# --- single writer ---


async def _memory_repo(tmp_path: Path) -> Path:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    repo = GitRepo(memory_dir)
    await repo.init()
    await repo.ensure_identity()
    return memory_dir


async def test_writer_applies_and_commits_sequentially(db: AsyncSession, tmp_path: Path) -> None:
    memory_dir = await _memory_repo(tmp_path)
    writer = MemoryWriter(db, memory_dir)
    run1 = await _make_run(db, "первый")
    run2 = await _make_run(db, "второй")

    await writer.enqueue(
        MemoryChangeRequest(
            file="user/profile.md",
            operation=MemoryOperation.CREATE,
            content="# Профиль\nлюбит Python\n",
            source_run_id=run1,
        )
    )
    await writer.enqueue(
        MemoryChangeRequest(
            file="user/profile.md",
            operation=MemoryOperation.APPEND,
            content="и краткость\n",
            source_run_id=run2,
        )
    )
    processed = await writer.drain()
    assert len(processed) == 2
    assert all(row.status is MemoryChangeStatus.APPLIED for row in processed)
    assert all(row.commit_sha for row in processed)

    text = (memory_dir / "user/profile.md").read_text(encoding="utf-8")
    assert "любит Python" in text
    assert "и краткость" in text

    # Каждая заявка — отдельный коммит с trailer Run-Id.
    repo = GitRepo(memory_dir)
    _, log, _ = await repo._git("log", "--format=%B", "-n", "2")
    assert f"Run-Id: {run2}" in log
    assert f"Run-Id: {run1}" in log


async def test_writer_blocks_commit_on_secret(db: AsyncSession, tmp_path: Path) -> None:
    memory_dir = await _memory_repo(tmp_path)
    writer = MemoryWriter(db, memory_dir)
    run_id = await _make_run(db, "секретный")
    # Заведомо ненастоящий токен (публичный AWS example).
    await writer.enqueue(
        MemoryChangeRequest(
            file="creds.md",
            operation=MemoryOperation.CREATE,
            content="ключ = AKIAIOSFODNN7EXAMPLE\n",
            source_run_id=run_id,
        )
    )
    processed = await writer.drain()
    assert processed[0].status is MemoryChangeStatus.FAILED
    assert processed[0].error is not None
    assert "secret" in processed[0].error.lower()


async def test_writer_second_drain_noop(db: AsyncSession, tmp_path: Path) -> None:
    memory_dir = await _memory_repo(tmp_path)
    writer = MemoryWriter(db, memory_dir)
    await writer.enqueue(
        MemoryChangeRequest(file="a.md", operation=MemoryOperation.CREATE, content="x\n")
    )
    await writer.drain()
    assert await writer.drain() == []
    remaining = (
        (
            await db.execute(
                select(MemoryChange).where(MemoryChange.status == MemoryChangeStatus.PENDING)
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []
