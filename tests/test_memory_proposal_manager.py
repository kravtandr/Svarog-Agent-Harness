"""Тесты memory-proposals (блок C, ADR-0020): отложенные правки памяти под ревью."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import (
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
