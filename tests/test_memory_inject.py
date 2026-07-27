"""Тесты блока авто-инъекции релевантной памяти (связка B).

Блок появляется только при переполнении index.md: на малой памяти поведение
контекста не меняется, а FTS включается там, где хвост каталога сворачивается
по дате и релевантные страницы выпадают из виду.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from svarog_harness.memory import index as memory_index
from svarog_harness.memory.inject import build_relevant_block
from svarog_harness.storage.db import create_engine, create_session_factory, init_db


def _many_projects(mem: Path, n: int) -> None:
    for i in range(n):
        d = mem / "projects" / f"p{i}"
        d.mkdir(parents=True)
        (d / "overview.md").write_text(
            f"---\nname: P{i}\nslug: p{i}\nsummary: s{i}\nstatus: active\n---\nтело проекта {i}\n",
            encoding="utf-8",
        )


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    mem = tmp_path / "memory"
    mem.mkdir()
    return mem


@pytest.fixture
async def sessions(
    tmp_path: Path, memory_dir: Path
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика к runtime-БД; индекс наполняется по фактическому состоянию памяти."""
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


async def _reindex(factory: async_sessionmaker[AsyncSession], memory_dir: Path) -> None:
    async with factory() as session:
        await memory_index.reindex(session, memory_dir)


async def test_no_block_when_not_overflowed(
    sessions: async_sessionmaker[AsyncSession], memory_dir: Path
) -> None:
    """Индекс влезает в потолок — блока нет, контекст как раньше."""
    _many_projects(memory_dir, 2)
    await _reindex(sessions, memory_dir)
    block = await build_relevant_block(
        sessions, memory_dir, "проекта 1", max_lines=200, pages=5, budget_bytes=3000
    )
    assert block == ""


async def test_block_on_overflow_contains_relevant(
    sessions: async_sessionmaker[AsyncSession], memory_dir: Path
) -> None:
    _many_projects(memory_dir, 40)
    await _reindex(sessions, memory_dir)
    block = await build_relevant_block(
        sessions, memory_dir, "тело проекта 7", max_lines=20, pages=5, budget_bytes=3000
    )
    assert "Релевантно задаче" in block
    assert "projects/p7/overview.md" in block
    assert len(block.encode("utf-8")) <= 3000


async def test_no_block_when_nothing_matches(
    sessions: async_sessionmaker[AsyncSession], memory_dir: Path
) -> None:
    """Переполнение есть, совпадений нет — блок не добавляется (шум не нужен)."""
    _many_projects(memory_dir, 40)
    await _reindex(sessions, memory_dir)
    block = await build_relevant_block(
        sessions, memory_dir, "несуществующее_слово_zzz", max_lines=20, pages=5, budget_bytes=3000
    )
    assert block == ""


async def test_block_respects_byte_budget(
    sessions: async_sessionmaker[AsyncSession], memory_dir: Path
) -> None:
    """Бюджет режет число строк, а не рвёт последнюю на середине."""
    _many_projects(memory_dir, 40)
    await _reindex(sessions, memory_dir)
    tight = await build_relevant_block(
        sessions, memory_dir, "тело проекта", max_lines=20, pages=5, budget_bytes=200
    )
    roomy = await build_relevant_block(
        sessions, memory_dir, "тело проекта", max_lines=20, pages=5, budget_bytes=3000
    )
    assert len(tight.encode("utf-8")) <= 200
    assert len(tight.splitlines()) < len(roomy.splitlines())
    # Каждая строка блока — целый пункт «- путь — фрагмент».
    assert all(line.startswith("- ") for line in tight.splitlines()[1:])


async def test_no_block_when_index_absent(
    sessions: async_sessionmaker[AsyncSession], memory_dir: Path
) -> None:
    """FTS-таблицы ещё нет (память не дренажилась) — fail-soft, без исключения."""
    _many_projects(memory_dir, 40)
    block = await build_relevant_block(
        sessions, memory_dir, "тело проекта 7", max_lines=20, pages=5, budget_bytes=3000
    )
    assert block == ""
