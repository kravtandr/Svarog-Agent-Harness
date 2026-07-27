"""Тесты tool search_memory (связка B): поиск по содержимому памяти."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from svarog_harness.memory import index as memory_index
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.tools.memory_tools import SearchMemoryArgs, SearchMemoryTool


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session-factory к runtime-БД с уже наполненным FTS-индексом."""
    mem = tmp_path / "memory"
    (mem / "decisions").mkdir(parents=True)
    (mem / "decisions" / "api.md").write_text(
        "# API\nверсионируем через заголовок X-Api-Version\n", encoding="utf-8"
    )
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        await memory_index.reindex(session, mem)
    yield factory
    await engine.dispose()


async def test_search_tool_returns_paths_and_snippets(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    tool = SearchMemoryTool(sessions)
    res = await tool.execute(SearchMemoryArgs(query="версионируем"))
    assert res.ok
    assert "decisions/api.md" in res.output
    # Сниппет обрамляет совпадение — агент видит контекст, не только путь.
    assert "[версионируем]" in res.output


async def test_search_tool_empty_result_is_friendly(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Ненайденное — это не ошибка tool'а: модель должна получить ok и текст."""
    tool = SearchMemoryTool(sessions)
    res = await tool.execute(SearchMemoryArgs(query="несуществующее_слово_zzz"))
    assert res.ok
    assert "ничего не найдено" in res.output.lower()


async def test_search_tool_malformed_query_is_friendly(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Запрос из одних спецсимволов схлопывается в пустой — просим уточнить."""
    tool = SearchMemoryTool(sessions)
    res = await tool.execute(SearchMemoryArgs(query='   ""  '))
    assert res.ok
    assert "уточни запрос" in res.output.lower()


async def test_search_tool_respects_limit(sessions: async_sessionmaker[AsyncSession]) -> None:
    tool = SearchMemoryTool(sessions)
    res = await tool.execute(SearchMemoryArgs(query="версионируем", limit=1))
    assert res.ok
    assert len(res.output.strip().splitlines()) == 1


def test_search_tool_is_read_only() -> None:
    """Read-only и LOW: как read_memory, approval не требуется (ADR-0004)."""
    tool = SearchMemoryTool(None)  # type: ignore[arg-type]
    assert tool.is_read_only(SearchMemoryArgs(query="x")) is True
