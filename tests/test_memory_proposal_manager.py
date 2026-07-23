"""Тесты memory-proposals (блок C, ADR-0020): отложенные правки памяти под ревью."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.proposal import MemoryProposalRequest
from svarog_harness.memory.proposal_manager import (
    MemoryProposalManager,
    MemoryProposalStateError,
)
from svarog_harness.memory.writer import MemoryWriter
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import (
    MemoryChange,
    MemoryChangeStatus,
    MemoryProposal,
    MemoryProposalOrigin,
    MemoryProposalStatus,
)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_memory_proposal_row_roundtrip(db: AsyncSession) -> None:
    """Строка proposal'а сохраняется и читается со всеми полями решения."""
    row = MemoryProposal(
        title="слить дубли проекта",
        rationale="две страницы описывают один бот",
        changes=[{"file": "projects/a/overview.md", "operation": "append", "content": "x"}],
        origin=MemoryProposalOrigin.DREAM,
        memory_head="deadbeef",
        checks={"validation": []},
    )
    db.add(row)
    await db.commit()

    found = (await db.execute(select(MemoryProposal))).scalar_one()
    assert found.status is MemoryProposalStatus.PENDING
    assert found.origin is MemoryProposalOrigin.DREAM
    assert found.title == "слить дубли проекта"
    assert found.changes[0]["file"] == "projects/a/overview.md"
    assert found.memory_head == "deadbeef"


def _request(*changes: MemoryChangeRequest) -> MemoryProposalRequest:
    return MemoryProposalRequest(title="правка", rationale="потому что", changes=tuple(changes))


def _append(file: str = "notes.md", content: str = "новый факт") -> MemoryChangeRequest:
    return MemoryChangeRequest(file=file, operation=MemoryOperation.APPEND, content=content)


async def test_persist_valid_proposal_is_pending(db: AsyncSession, tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    row = await MemoryProposalManager(db, mem).persist(_request(_append()))
    assert row.status is MemoryProposalStatus.PENDING
    assert row.changes[0]["operation"] == "append"


async def test_persist_invalid_proposal_is_failed(db: AsyncSession, tmp_path: Path) -> None:
    """Невалидный proposal не теряется: он записан со статусом failed и причинами."""
    mem = tmp_path / "memory"
    mem.mkdir()
    row = await MemoryProposalManager(db, mem).persist(
        MemoryProposalRequest(title="", rationale="", changes=())
    )
    assert row.status is MemoryProposalStatus.FAILED
    assert MemoryProposalManager.validation_messages(row)


async def test_approve_enqueues_changes(db: AsyncSession, tmp_path: Path) -> None:
    """Одобрение перекладывает заявки в штатную очередь единственного писателя."""
    mem = tmp_path / "memory"
    mem.mkdir()
    manager = MemoryProposalManager(db, mem)
    row = await manager.persist(_request(_append(), _append("other.md")))

    ids = await manager.decide(row, approved=True, decided_by="cli")

    assert len(ids) == 2
    assert row.status is MemoryProposalStatus.APPLIED
    assert row.applied_change_ids == ids
    queued = (await db.execute(select(MemoryChange))).scalars().all()
    assert [q.status for q in queued] == [MemoryChangeStatus.PENDING] * 2


async def test_reject_touches_nothing(db: AsyncSession, tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    manager = MemoryProposalManager(db, mem)
    row = await manager.persist(_request(_append()))

    ids = await manager.decide(row, approved=False, decided_by="cli", reason="не надо")

    assert ids == []
    assert row.status is MemoryProposalStatus.REJECTED
    assert row.reason == "не надо"
    assert (await db.execute(select(MemoryChange))).scalars().all() == []
    assert not (mem / "notes.md").exists()


async def test_second_decision_rejected(db: AsyncSession, tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    manager = MemoryProposalManager(db, mem)
    row = await manager.persist(_request(_append()))
    await manager.decide(row, approved=False, decided_by="cli")

    with pytest.raises(MemoryProposalStateError):
        await manager.decide(row, approved=True, decided_by="cli")


async def test_pending_count_counts_only_pending(db: AsyncSession, tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    manager = MemoryProposalManager(db, mem)
    first = await manager.persist(_request(_append()))
    await manager.persist(_request(_append("second.md")))
    await manager.decide(first, approved=False, decided_by="cli")

    assert await manager.pending_count() == 1


async def test_preview_is_computed_on_current_state(db: AsyncSession, tmp_path: Path) -> None:
    """Предпросмотр считается на текущей памяти, а не замораживается при создании."""
    mem = tmp_path / "memory"
    mem.mkdir()
    manager = MemoryProposalManager(db, mem)
    row = await manager.persist(_request(_append(content="хвост")))

    # Память ушла вперёд уже после предложения.
    (mem / "notes.md").write_text("голова\n", encoding="utf-8")

    previews = manager.preview(row)
    assert len(previews) == 1
    path, text = previews[0]
    assert path == "notes.md"
    assert "голова" in text and "хвост" in text


async def test_preview_reports_inapplicable_change(db: AsyncSession, tmp_path: Path) -> None:
    """Неприменимая правка показывается человеку как таковая, а не роняет show."""
    mem = tmp_path / "memory"
    mem.mkdir()
    manager = MemoryProposalManager(db, mem)
    row = await manager.persist(_request(_append()))
    row.changes = [
        {
            "file": "gone.md",
            "operation": "replace_section",
            "content": "тело",
            "section": "Раздел",
            "field": "",
        }
    ]

    previews = manager.preview(row)
    assert "не применима" in previews[0][1]


async def test_applied_changes_reach_files_after_drain(db: AsyncSession, tmp_path: Path) -> None:
    """Сквозной путь: approve → очередь → drain → файл на диске.

    Каталог памяти здесь настоящий git-репозиторий: writer коммитит каждую
    применённую заявку (Flow A, ADR-0004), и без репозитория этот путь
    проверялся бы не целиком.
    """
    mem = tmp_path / "memory"
    mem.mkdir()
    repo = GitRepo(mem)
    await repo.init()
    await repo.ensure_identity()
    (mem / ".gitkeep").write_text("", encoding="utf-8")
    await repo.add_all()
    await repo.commit("init memory")
    manager = MemoryProposalManager(db, mem)
    row = await manager.persist(_request(_append(content="строка")))
    await manager.decide(row, approved=True, decided_by="cli")

    await MemoryWriter(db, mem).drain()

    assert "строка" in (mem / "notes.md").read_text(encoding="utf-8")
