"""Тесты FTS-ядра памяти (связка B): schema, reindex, search, санитизация."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.memory import index as mi
from svarog_harness.storage.db import create_engine, create_session_factory, init_db


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


def _seed(memory_dir: Path) -> None:
    (memory_dir / "projects" / "billing").mkdir(parents=True)
    (memory_dir / "projects" / "billing" / "overview.md").write_text(
        "---\nname: Billing\nslug: billing\nsummary: счета\nstatus: active\n---\n"
        "решили версионировать API через заголовок X-Api-Version\n",
        encoding="utf-8",
    )
    (memory_dir / "decisions").mkdir()
    (memory_dir / "decisions" / "retries.md").write_text(
        "# Ретраи\nэкспоненциальный бэкофф, максимум пять попыток\n", encoding="utf-8"
    )
    (memory_dir / "user").mkdir()
    (memory_dir / "user" / "profile.md").write_text("## Тон\nкратко\n", encoding="utf-8")


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    mem = tmp_path / "memory"
    mem.mkdir()
    _seed(mem)
    return mem


# --- sanitize_query ---


def test_sanitize_neutralizes_operators() -> None:
    """Сырой ввод модели содержит операторы FTS5 — они не должны доходить до MATCH."""
    assert mi.sanitize_query("") == ""
    assert mi.sanitize_query("   ") == ""
    assert '"api"' in mi.sanitize_query("api OR (drop)")
    # Кавычки в запросе не могут разорвать литерал.
    assert mi.sanitize_query('он сказал "нет"') == '"он" "сказал" "нет"'


# --- reindex + search ---


async def test_reindex_and_search_finds_by_content(db: AsyncSession, memory_dir: Path) -> None:
    await mi.reindex(db, memory_dir)
    hits = await mi.search(db, "версионировать", limit=5)
    assert "projects/billing/overview.md" in [h.path for h in hits]
    assert hits[0].snippet


async def test_snippet_is_single_line(db: AsyncSession, memory_dir: Path) -> None:
    """Сниппет многострочной страницы схлопнут: построчный формат выдачи цел."""
    await mi.reindex(db, memory_dir)
    hits = await mi.search(db, "версионировать", limit=5)
    assert "\n" not in hits[0].snippet


async def test_search_is_case_insensitive_cyrillic(db: AsyncSession, memory_dir: Path) -> None:
    """unicode61 приводит регистр — кириллица ищется без учёта регистра."""
    await mi.reindex(db, memory_dir)
    hits = await mi.search(db, "БЭКОФФ", limit=5)
    assert "decisions/retries.md" in [h.path for h in hits]


async def test_reindex_is_idempotent(db: AsyncSession, memory_dir: Path) -> None:
    """Повторный ребилд не плодит дубли строк (полная перестройка таблицы)."""
    await mi.reindex(db, memory_dir)
    await mi.reindex(db, memory_dir)
    hits = await mi.search(db, "версионировать", limit=10)
    assert [h.path for h in hits].count("projects/billing/overview.md") == 1


async def test_reindex_drops_deleted_pages(db: AsyncSession, memory_dir: Path) -> None:
    """Удалённая страница пропадает из индекса после следующего ребилда."""
    await mi.reindex(db, memory_dir)
    (memory_dir / "decisions" / "retries.md").unlink()
    await mi.reindex(db, memory_dir)
    assert await mi.search(db, "бэкофф", limit=5) == []


async def test_profile_and_autogen_not_indexed(db: AsyncSession, memory_dir: Path) -> None:
    """index.md/log.md — навигация, profile.md всегда в контексте: не индексируем."""
    (memory_dir / "index.md").write_text("# Индекс памяти\nсчета\n", encoding="utf-8")
    (memory_dir / "log.md").write_text("## [2026-07-24] create | x | run y\n", encoding="utf-8")
    await mi.reindex(db, memory_dir)
    hits = await mi.search(db, "кратко", limit=5)
    assert all("profile.md" not in h.path and "index.md" not in h.path for h in hits)
    assert await mi.search(db, "log.md", limit=5) == []


# --- fail-soft ---


async def test_search_empty_query_and_no_table(db: AsyncSession) -> None:
    """Пустой запрос и отсутствующая таблица дают [], а не исключение."""
    assert await mi.search(db, "", limit=5) == []
    assert await mi.search(db, "нечто", limit=5) == []


async def test_search_malformed_query_does_not_raise(db: AsyncSession, memory_dir: Path) -> None:
    """Спецсинтаксис FTS5 в запросе не роняет MATCH — санитизация держит удар."""
    await mi.reindex(db, memory_dir)
    assert await mi.search(db, 'NEAR("a" "b", 2) AND *', limit=5) == []
    assert await mi.search(db, "версионировать API", limit=5)


async def test_search_is_conjunctive_over_tokens(db: AsyncSession, memory_dir: Path) -> None:
    """Санитизация даёт AND по всем токенам: лишнее слово в запросе сужает выдачу.

    Плата за нейтрализацию операторов — служебные слова модели («OR», «где»)
    становятся обязательными термами. Отмечено как ограничение, не баг.
    """
    await mi.reindex(db, memory_dir)
    assert await mi.search(db, "версионировать", limit=5)
    assert await mi.search(db, "версионировать бэкофф", limit=5) == []
