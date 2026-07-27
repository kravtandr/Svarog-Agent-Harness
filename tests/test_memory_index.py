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
        "решили версионировать API через заголовок X-Api-Version;\n"
        "интеграция с эквайрингом, сверка платежей\n",
        encoding="utf-8",
    )
    # Короткая страница, делящая с billing одно слово: на ней видно, что точный
    # проход идёт раньше широкого (иначе bm25 вынесет её вперёд как более короткую).
    (memory_dir / "projects" / "crm").mkdir(parents=True)
    (memory_dir / "projects" / "crm" / "overview.md").write_text(
        "---\nname: CRM\nslug: crm\nsummary: клиенты\nstatus: active\n---\n"
        "интеграция с телефонией\n",
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
    assert mi.sanitize_query('версия "схемы"') == '"версия"* OR "схемы"*'


def test_sanitize_is_disjunctive_with_prefix() -> None:
    """Токены соединяются OR и ищутся по префиксу — порядок держит bm25.

    AND обнулял выдачу от любого служебного слова, а точная словоформа не
    находила словоизменение («эквайринг» мимо «эквайрингом»).
    """
    assert mi.sanitize_query("версионирование API") == '"версионирование"* OR "API"*'


def test_sanitize_drops_stopwords() -> None:
    """Предлоги и местоимения выброшены: иначе «к» совпадает почти со всем.

    Регрессия: без этого запрос без общих слов со страницей всё равно давал
    совпадение по предлогу, и короткая нерелевантная страница выигрывала bm25.
    """
    assert mi.sanitize_query("а как у нас устроена сверка счетов") == (
        '"устроена"* OR "сверка"* OR "счетов"*'
    )
    # Запрос из одних служебных слов искать нечем.
    assert mi.sanitize_query("а что у нас про это") == ""


def test_sanitize_keeps_short_meaningful_tokens_exact() -> None:
    """Префикс на 1-2 символах — маска по словарю, но сам токен терять нельзя."""
    assert mi.sanitize_query("схема БД") == '"схема"* OR "БД"'


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


async def test_noise_words_do_not_zero_the_result(db: AsyncSession, memory_dir: Path) -> None:
    """Служебные слова модели не обнуляют выдачу: откат в OR, когда AND пуст."""
    await mi.reindex(db, memory_dir)
    hits = await mi.search(db, "где мы договаривались про версионирование API", limit=5)
    assert hits, "естественная формулировка не должна давать пусто"
    assert hits[0].path == "projects/billing/overview.md", "релевантная страница — первой"


async def test_precise_query_is_not_diluted_by_or(db: AsyncSession, memory_dir: Path) -> None:
    """Есть страница со ВСЕМИ термами — отдаём только её, без OR-хвоста.

    `интеграция` есть и в billing, и в crm; `эквайрингом` — только в billing.
    Чистый OR вернул бы обе и вынес crm вперёд (короче → выше bm25).
    """
    await mi.reindex(db, memory_dir)
    hits = await mi.search(db, "интеграция с эквайрингом", limit=5)
    assert [h.path for h in hits] == ["projects/billing/overview.md"]


async def test_query_without_common_words_returns_nothing(
    db: AsyncSession, memory_dir: Path
) -> None:
    """Нет пересечения по значимым словам — честная пустота, а не мусор.

    Регрессия: совпадение по предлогу выносило наверх короткую нерелевантную
    страницу. Пустая выдача заставляет агента переформулировать (S35), а
    уверенный неверный ответ — нет.
    """
    await mi.reindex(db, memory_dir)
    assert await mi.search(db, "сколько раз мы повторяем запрос при сбое", limit=5) == []


async def test_search_finds_inflected_form(db: AsyncSession, memory_dir: Path) -> None:
    """Запрос в начальной форме находит словоизменение: «эквайринг» → «эквайрингом».

    Наращение окончания закрывает префиксный поиск. Смена основы
    («счета» → «счетов») ему не поддаётся — это граница лексического подхода.
    """
    await mi.reindex(db, memory_dir)
    hits = await mi.search(db, "эквайринг", limit=5)
    assert "projects/billing/overview.md" in [h.path for h in hits]
