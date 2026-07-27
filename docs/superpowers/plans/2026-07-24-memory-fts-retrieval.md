# FTS5-retrieval памяти (связка B) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать агенту поиск памяти по содержимому (`search_memory` tool) и
авто-инъекцию релевантных страниц при переполнении индекса, поверх SQLite FTS5.

**Architecture:** Общее ядро `MemoryIndex` (async, на `AsyncSession`) владеет
FTS5-таблицей `memory_fts` в runtime-БД; единственный writer в `_reindex`
перестраивает её после каждого дренажа. Tool `search_memory` и native
авто-инъекция читают через инъектированную session-factory. Внешние executor'ы
получают поиск через тот же tool в бридже.

**Tech Stack:** Python 3.12, SQLite FTS5 (часть sqlite, `unicode61`),
SQLAlchemy async (`text()` для FTS SQL), pytest + pytest-asyncio.

## Global Constraints

- Инфраструктура — только Git + SQLite; FTS5 — часть sqlite, вектор/эмбеддинги
  запрещены (ADR-0001).
- FTS-индекс — производное состояние в runtime-БД (`.svarog/svarog.db`), в Git
  не коммитится, перестраивается из файлов.
- Всё fail-soft: сбой FTS (нет расширения, кривой запрос, рассинхрон) НИКОГДА не
  роняет run — деградирует до «поиск недоступен» / пустого результата.
- Пишется только через единственный writer (ADR-0004): FTS-запись идёт через
  writer-сессию в `_reindex`; tool/инъекция — только чтение.
- Индексируются `*.md` кроме `index.md`, `log.md`, `user/profile.md`.
- Русские докстринги; `ruff`/`ruff format`/`mypy` чисты; тесты по образцу
  `tests/test_memory_*.py`.

---

### Task 1: FTS-поля в `MemoryConfig`

**Files:**
- Modify: `src/svarog_harness/config/schema.py` (`MemoryConfig`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `MemoryConfig.fts_enabled: bool = True`,
  `MemoryConfig.fts_inject_pages: int = 5` (gt=0),
  `MemoryConfig.fts_inject_bytes: int = 3000` (gt=0).

- [x] **Step 1: Написать падающий тест**

```python
# tests/test_config.py — добавить
def test_memory_fts_defaults() -> None:
    from svarog_harness.config.schema import MemoryConfig

    cfg = MemoryConfig()
    assert cfg.fts_enabled is True
    assert cfg.fts_inject_pages == 5
    assert cfg.fts_inject_bytes == 3000
```

- [x] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/test_config.py -k memory_fts -v`
Expected: FAIL — `AttributeError: ... has no attribute 'fts_enabled'`.

- [x] **Step 3: Реализация**

В `MemoryConfig` (после `index_max_lines`) добавить:

```python
    # FTS5-retrieval (связка B): полнотекстовый индекс памяти в runtime-БД.
    fts_enabled: bool = True
    # Авто-инъекция при переполнении index.md: K страниц и байтовый бюджет блока.
    fts_inject_pages: int = Field(default=5, gt=0)
    fts_inject_bytes: int = Field(default=3000, gt=0)
```

- [x] **Step 4: Запустить тесты**

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS.

- [x] **Step 5: Коммит**

```bash
git add src/svarog_harness/config/schema.py tests/test_config.py
git commit -m "feat(config): FTS-поля MemoryConfig (связка B)"
```

---

### Task 2: `wiki.index_overflowed` (сигнал переполнения)

Выделяем построение строк индекса в общий хелпер и добавляем проверку
переполнения — её использует авто-инъекция.

**Files:**
- Modify: `src/svarog_harness/memory/wiki.py`
- Test: `tests/test_memory_wiki.py`

**Interfaces:**
- Produces: `index_overflowed(memory_dir: Path, *, max_lines: int) -> bool` —
  `True`, если полный (несвёрнутый) индекс превышает `max_lines` строк.

- [x] **Step 1: Написать падающий тест**

```python
# tests/test_memory_wiki.py — добавить
from svarog_harness.memory.wiki import index_overflowed


def test_index_overflowed_false_when_small(tmp_path: Path) -> None:
    (tmp_path / "projects" / "a").mkdir(parents=True)
    (tmp_path / "projects" / "a" / "overview.md").write_text(
        "---\nname: A\nslug: a\nsummary: s\nstatus: active\n---\n", encoding="utf-8"
    )
    assert index_overflowed(tmp_path, max_lines=200) is False


def test_index_overflowed_true_when_many(tmp_path: Path) -> None:
    for i in range(60):
        d = tmp_path / "projects" / f"p{i}"
        d.mkdir(parents=True)
        (d / "overview.md").write_text(
            f"---\nname: P{i}\nslug: p{i}\nsummary: s\nstatus: active\n---\n", encoding="utf-8"
        )
    assert index_overflowed(tmp_path, max_lines=20) is True
```

- [x] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/test_memory_wiki.py -k overflow -v`
Expected: FAIL — `ImportError: cannot import name 'index_overflowed'`.

- [x] **Step 3: Реализация**

В `wiki.py` вынести построение строк из `render_index` в `_index_lines` и
добавить `index_overflowed`. Заменить начало `render_index`:

```python
def _index_lines(memory_dir: Path) -> list[str]:
    """Полный список строк индекса (без потолка) — общий для render/overflow."""
    pages = _project_pages(memory_dir)
    active = sorted(
        (p for p in pages if p["status"] in _ACTIVE_STATUSES),
        key=lambda p: p["updated"],
        reverse=True,
    )
    archived = [p for p in pages if p["status"] == "archived"]
    lines = [f"# Индекс памяти\n{_AUTOGEN}", "", "## Проекты"]
    lines += [_project_line(p) for p in active] or ["_(пока нет проектов)_"]
    if archived:
        lines += ["", "## Проекты (архив)"]
        lines += [_project_line(p) for p in archived]
    decisions = _decision_pages(memory_dir)
    if decisions:
        lines += ["", "## Решения"]
        for title, summary, path in decisions:
            tail = f" — {summary}" if summary else ""
            lines.append(f"- [{title}]({path}){tail}")
    sources = _source_files(memory_dir)
    if sources:
        lines += ["", "## Источники"]
        for rel in sources:
            label = rel.removeprefix("sources/")
            lines.append(f"- [{label}]({rel})")
    if (memory_dir / "user" / "profile.md").exists():
        lines += ["", "## Пользователь", "- [Профиль](user/profile.md)"]
    return [_clip_line(line) for line in "\n".join(lines).split("\n")]


def index_overflowed(memory_dir: Path, *, max_lines: int = _DEFAULT_MAX_LINES) -> bool:
    """Превышает ли полный индекс потолок строк (сигнал для авто-инъекции)."""
    return len(_index_lines(memory_dir)) > max_lines


def render_index(memory_dir: Path, *, max_lines: int = _DEFAULT_MAX_LINES) -> str:
    """Собрать текст index.md из текущего состояния памяти."""
    lines = _index_lines(memory_dir)
    if len(lines) > max_lines:
        dropped = lines[max_lines - 1 :]
        dropped_pages = sum(1 for line in dropped if line.startswith("- "))
        lines = [
            *lines[: max_lines - 1],
            f"> …и ещё {dropped_pages} страниц — полный список см. read_memory",
        ]
    return "\n".join(lines).rstrip("\n") + "\n"
```

- [x] **Step 4: Запустить тесты**

Run: `uv run pytest tests/test_memory_wiki.py -q`
Expected: PASS (новые + старые про render_index).

- [x] **Step 5: Коммит**

```bash
git add src/svarog_harness/memory/wiki.py tests/test_memory_wiki.py
git commit -m "feat(memory): index_overflowed + вынос _index_lines (связка B)"
```

---

### Task 3: Ядро `MemoryIndex` (FTS5)

**Files:**
- Create: `src/svarog_harness/memory/index.py`
- Test: `tests/test_memory_index.py`

**Interfaces:**
- Consumes: `AsyncSession`; `sqlalchemy.text`.
- Produces:
  - `@dataclass(frozen=True) SearchHit: path: str; snippet: str`
  - `sanitize_query(raw: str) -> str` — токены в кавычки (нейтрализует
    FTS5-операторы); пустой ввод → `""`.
  - `async ensure_schema(session) -> None`
  - `async reindex(session, memory_dir: Path) -> None` — полный ребилд таблицы
    из индексируемых файлов (idempotent).
  - `async search(session, query: str, *, limit: int) -> list[SearchHit]` —
    `""`/нет таблицы/пустой запрос → `[]`.

- [x] **Step 1: Написать падающий тест**

```python
# tests/test_memory_index.py
"""Тесты FTS-ядра MemoryIndex (связка B)."""

from pathlib import Path

import pytest

from svarog_harness.memory import index as mi
from svarog_harness.storage.db import create_engine, create_session_factory, init_db


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


async def _session(tmp_path: Path):
    init_db(tmp_path / "db.sqlite3")
    engine = create_engine(tmp_path / "db.sqlite3")
    return create_session_factory(engine)()


def test_sanitize_neutralizes_operators() -> None:
    assert mi.sanitize_query("") == ""
    # операторы/кавычки не роняют — токены заkey в кавычки
    assert '"api"' in mi.sanitize_query("api OR (drop)")


@pytest.mark.asyncio
async def test_reindex_and_search_finds_by_content(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    _seed(mem)
    async with await _session(tmp_path) as s:
        await mi.reindex(s, mem)
        hits = await mi.search(s, "версионировать", limit=5)
        paths = [h.path for h in hits]
        assert "projects/billing/overview.md" in paths
        assert hits[0].snippet  # непустой сниппет


@pytest.mark.asyncio
async def test_profile_and_autogen_not_indexed(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    _seed(mem)
    (mem / "index.md").write_text("# Индекс памяти\nсчета\n", encoding="utf-8")
    async with await _session(tmp_path) as s:
        await mi.reindex(s, mem)
        # запрос по слову из профиля/индекса не должен возвращать их пути
        hits = await mi.search(s, "кратко", limit=5)
        assert all("profile.md" not in h.path and "index.md" not in h.path for h in hits)


@pytest.mark.asyncio
async def test_search_empty_query_and_no_table(tmp_path: Path) -> None:
    async with await _session(tmp_path) as s:
        assert await mi.search(s, "", limit=5) == []
        # таблицы ещё нет → пусто, без исключения
        assert await mi.search(s, "нечто", limit=5) == []
```

- [x] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/test_memory_index.py -q`
Expected: FAIL — `ModuleNotFoundError: ...memory.index`.

- [x] **Step 3: Реализация**

```python
# src/svarog_harness/memory/index.py
"""FTS5-индекс памяти (связка B, #2): поиск по содержимому страниц.

Производное состояние в runtime-БД (не в Git). Единственный writer
перестраивает индекс в `_reindex`; tool `search_memory` и авто-инъекция — только
чтение. Всё fail-soft: нет расширения/таблицы/кривой запрос → пустой результат,
не исключение (retrieval не должен ронять run).
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

# Автоген и профиль не индексируем: навигация/всегда-в-контексте.
_SKIP = frozenset({"index.md", "log.md", "user/profile.md"})

_CREATE = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts "
    "USING fts5(path, content, tokenize='unicode61')"
)


@dataclass(frozen=True)
class SearchHit:
    path: str
    snippet: str


def sanitize_query(raw: str) -> str:
    """Свести запрос к списку токенов в кавычках — нейтрализует FTS5-операторы.

    FTS5 MATCH трактует OR/AND/NEAR/скобки/кавычки как синтаксис; сырой ввод
    модели их содержит и роняет запрос. Берём слова, экранируем кавычки
    удвоением, оборачиваем каждое — получается безопасный AND-поиск по словам.
    """
    tokens = [t for t in raw.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in tokens)


def _indexed_files(memory_dir: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for md in sorted(memory_dir.rglob("*.md")):
        rel = md.relative_to(memory_dir).as_posix()
        if rel in _SKIP:
            continue
        try:
            out.append((rel, md.read_text(encoding="utf-8")))
        except OSError:
            continue
    return out


async def ensure_schema(session: AsyncSession) -> None:
    await session.execute(text(_CREATE))


async def reindex(session: AsyncSession, memory_dir: Path) -> None:
    """Полный ребилд FTS-таблицы из индексируемых файлов (idempotent)."""
    await ensure_schema(session)
    await session.execute(text("DELETE FROM memory_fts"))
    for rel, content in _indexed_files(memory_dir):
        await session.execute(
            text("INSERT INTO memory_fts(path, content) VALUES (:p, :c)"),
            {"p": rel, "c": content},
        )
    await session.commit()


async def search(session: AsyncSession, query: str, *, limit: int) -> list[SearchHit]:
    """Top-N страниц по содержимому (bm25). Fail-soft: ошибки → []."""
    q = sanitize_query(query)
    if not q:
        return []
    try:
        rows = (
            await session.execute(
                text(
                    "SELECT path, snippet(memory_fts, 1, '[', ']', '…', 12) AS snip "
                    "FROM memory_fts WHERE memory_fts MATCH :q ORDER BY rank LIMIT :lim"
                ),
                {"q": q, "lim": limit},
            )
        ).all()
    except OperationalError:
        # нет таблицы / нет FTS5-расширения — retrieval недоступен, деградируем.
        return []
    return [SearchHit(path=r[0], snippet=r[1]) for r in rows]
```

- [x] **Step 4: Запустить тесты**

Run: `uv run pytest tests/test_memory_index.py -q`
Expected: PASS (все).

- [x] **Step 5: Коммит**

```bash
git add src/svarog_harness/memory/index.py tests/test_memory_index.py
git commit -m "feat(memory): ядро MemoryIndex — FTS5 schema/reindex/search (связка B)"
```

---

### Task 4: Writer синхронизирует FTS в `_reindex`

**Files:**
- Modify: `src/svarog_harness/memory/writer.py` (`__init__`, `_reindex`)
- Modify: `src/svarog_harness/runtime/orchestrator.py` (передать `fts_enabled` в
  оба конструктора `MemoryWriter` — `drain_memory` и `autocapture`)
- Test: `tests/test_memory.py`

**Interfaces:**
- Consumes: `memory.index.reindex` (Task 3).
- Produces: `MemoryWriter(..., fts_enabled: bool = True)` — при `True` после
  автогена index.md вызывает `await index.reindex(self._db, self._memory_dir)`.

- [x] **Step 1: Написать падающий тест**

```python
# tests/test_memory.py — добавить (writer синкает FTS, search находит)
import pytest
from svarog_harness.memory import index as mi
from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.writer import MemoryWriter
from svarog_harness.storage.db import create_engine, create_session_factory, init_db


@pytest.mark.asyncio
async def test_writer_reindex_populates_fts(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    # memory — git-репо (writer коммитит)
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=mem, check=True)
    init_db(tmp_path / "db.sqlite3")
    engine = create_engine(tmp_path / "db.sqlite3")
    async with create_session_factory(engine)() as db:
        writer = MemoryWriter(db, mem, index_max_lines=200, fts_enabled=True)
        await writer.enqueue(
            MemoryChangeRequest(
                file="decisions/api.md",
                operation=MemoryOperation.CREATE,
                content="# API\nверсионируем через заголовок X-Api-Version\n",
            )
        )
        await writer.drain()
        hits = await mi.search(db, "версионируем", limit=5)
        assert any(h.path == "decisions/api.md" for h in hits)
```

- [x] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/test_memory.py -k reindex_populates_fts -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'fts_enabled'`.

- [x] **Step 3: Реализация**

В `writer.py` — импорт и параметр:

```python
from svarog_harness.memory import index as memory_index
```

В `MemoryWriter.__init__` добавить параметр и поле:

```python
    def __init__(
        self,
        db: AsyncSession,
        memory_dir: Path,
        *,
        lock: LockBackend | None = None,
        index_max_lines: int = 200,
        fts_enabled: bool = True,
    ) -> None:
        ...
        self._index_max_lines = index_max_lines
        self._fts_enabled = fts_enabled
```

В конце `_reindex` (после блока коммита index.md) добавить синк FTS:

```python
        with contextlib.suppress(SecretScanBlockedError):
            await commit_guarded(self._repo, "memory: reindex", known_values=known_values)
        # FTS-индекс (связка B): производное состояние в runtime-БД, не в git.
        # Полный ребилд под той же writer-сессией — консистентно, без гонок.
        if self._fts_enabled:
            with contextlib.suppress(Exception):
                await memory_index.reindex(self._db, self._memory_dir)
```

> `contextlib.suppress(Exception)` вокруг FTS: сбой индекса (нет расширения и
> т.п.) не должен ронять дренаж памяти — retrieval деградирует, память цела.

В `orchestrator.py` — в `drain_memory` и `autocapture` при создании
`MemoryWriter(...)` добавить `fts_enabled=self._cfg.memory.fts_enabled`:

```python
            writer = MemoryWriter(
                db,
                mem_dir,
                lock=self._lock,
                index_max_lines=self._cfg.memory.index_max_lines,
                fts_enabled=self._cfg.memory.fts_enabled,
            )
```

- [x] **Step 4: Запустить тесты**

Run: `uv run pytest tests/test_memory.py -q`
Expected: PASS.

- [x] **Step 5: Коммит**

```bash
git add src/svarog_harness/memory/writer.py src/svarog_harness/runtime/orchestrator.py tests/test_memory.py
git commit -m "feat(memory): writer синкает FTS-индекс в _reindex (связка B)"
```

---

### Task 5: Tool `search_memory`

**Files:**
- Modify: `src/svarog_harness/tools/memory_tools.py` (`SearchMemoryTool`)
- Test: `tests/test_memory_search_tool.py`

**Interfaces:**
- Consumes: `memory.index.search` (Task 3); `async_sessionmaker` (session
  factory для чтения).
- Produces: `SearchMemoryTool(session_factory, *, limit_default: int = 5)` —
  read-only, LOW; `execute(SearchMemoryArgs)` возвращает пути+сниппеты.

- [x] **Step 1: Написать падающий тест**

```python
# tests/test_memory_search_tool.py
"""Тесты tool search_memory (связка B)."""

from pathlib import Path

import pytest

from svarog_harness.memory import index as mi
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.tools.memory_tools import SearchMemoryArgs, SearchMemoryTool


async def _factory_with_data(tmp_path: Path):
    mem = tmp_path / "memory"
    (mem / "decisions").mkdir(parents=True)
    (mem / "decisions" / "api.md").write_text(
        "# API\nверсионируем через заголовок X-Api-Version\n", encoding="utf-8"
    )
    init_db(tmp_path / "db.sqlite3")
    factory = create_session_factory(create_engine(tmp_path / "db.sqlite3"))
    async with factory() as s:
        await mi.reindex(s, mem)
    return factory


@pytest.mark.asyncio
async def test_search_tool_returns_paths_and_snippets(tmp_path: Path) -> None:
    factory = await _factory_with_data(tmp_path)
    tool = SearchMemoryTool(factory)
    res = await tool.execute(SearchMemoryArgs(query="версионируем"))
    assert res.ok
    assert "decisions/api.md" in res.output


@pytest.mark.asyncio
async def test_search_tool_empty_result_is_friendly(tmp_path: Path) -> None:
    factory = await _factory_with_data(tmp_path)
    tool = SearchMemoryTool(factory)
    res = await tool.execute(SearchMemoryArgs(query="несуществующее_слово_zzz"))
    assert res.ok
    assert "ничего не найдено" in res.output.lower()


@pytest.mark.asyncio
async def test_search_tool_malformed_query_is_friendly(tmp_path: Path) -> None:
    factory = await _factory_with_data(tmp_path)
    tool = SearchMemoryTool(factory)
    res = await tool.execute(SearchMemoryArgs(query='   ""  '))
    assert res.ok
    assert "уточни запрос" in res.output.lower()
```

- [x] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/test_memory_search_tool.py -q`
Expected: FAIL — `ImportError: cannot import name 'SearchMemoryTool'`.

- [x] **Step 3: Реализация**

В `tools/memory_tools.py` добавить импорты и класс:

```python
from sqlalchemy.ext.asyncio import async_sessionmaker

from svarog_harness.memory import index as memory_index
```

```python
class SearchMemoryArgs(BaseModel):
    query: str = Field(description="Ключевые слова для поиска по содержимому памяти")
    limit: int = Field(default=5, ge=1, le=20, description="Сколько результатов вернуть")


class SearchMemoryTool(Tool[SearchMemoryArgs]):
    """Полнотекстовый поиск по памяти (связка B): найти страницы по содержимому.

    Read-only, LOW. Возвращает пути + сниппеты; полную страницу подтягивай через
    read_memory. Список страниц по заголовкам — в index.md; этот tool ищет по
    тексту, когда по заголовку не найти.
    """

    name = "search_memory"
    action_type = "memory.read"
    description = (
        "Полнотекстовый поиск по долговременной памяти (страницы проектов, "
        "решения, источники) по содержимому. Возвращает пути и фрагменты; "
        "полную страницу читай через read_memory. Ищи, когда по заголовку в "
        "index.md нужную страницу не найти."
    )
    risk_level = RiskLevel.LOW
    args_model = SearchMemoryArgs

    def is_read_only(self, args: SearchMemoryArgs) -> bool:
        return True

    def __init__(self, session_factory: "async_sessionmaker") -> None:
        self._sessions = session_factory

    async def execute(self, args: SearchMemoryArgs) -> ToolResult:
        if not memory_index.sanitize_query(args.query):
            return ToolResult.success("уточни запрос: нужны ключевые слова для поиска")
        async with self._sessions() as session:
            hits = await memory_index.search(session, args.query, limit=args.limit)
        if not hits:
            return ToolResult.success("ничего не найдено по запросу")
        lines = [f"- {h.path} — {h.snippet}" for h in hits]
        return ToolResult.success("\n".join(lines))
```

- [x] **Step 4: Запустить тесты**

Run: `uv run pytest tests/test_memory_search_tool.py -q`
Expected: PASS (все).

- [x] **Step 5: Коммит**

```bash
git add src/svarog_harness/tools/memory_tools.py tests/test_memory_search_tool.py
git commit -m "feat(tools): search_memory — FTS-поиск по памяти (связка B)"
```

---

### Task 6: Регистрация `search_memory` (native registry + бридж)

**Files:**
- Modify: `src/svarog_harness/runtime/run_assembly.py` (lazy read
  session-factory; регистрация в `_build_registry` — DREAM и default)
- Modify: `src/svarog_harness/runtime/bridge_control.py` (`_build_tools`)
- Test: `tests/test_dream_profile.py` (реестр DREAM содержит search_memory)

**Interfaces:**
- Consumes: `SearchMemoryTool` (Task 5); `create_engine`,
  `create_session_factory` (`storage/db.py`).
- Produces: `RunAssembly._read_session_factory() -> async_sessionmaker`
  (ленивая, кэш).

- [x] **Step 1: Написать падающий тест**

```python
# tests/test_dream_profile.py — добавить
def test_dream_registry_has_search_memory(tmp_path: Path) -> None:
    names = _names(tmp_path, RunProfile.DREAM)
    assert "search_memory" in names
```

- [x] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/test_dream_profile.py -k search_memory -v`
Expected: FAIL — `search_memory` не в реестре.

- [x] **Step 3: Реализация**

В `run_assembly.py` — импорт и ленивая фабрика (в `__init__` добавить
`self._read_sessions_cache = None`):

```python
from svarog_harness.storage.db import create_engine, create_session_factory
from svarog_harness.tools.memory_tools import SearchMemoryTool
```

```python
    def _read_session_factory(self):  # -> async_sessionmaker
        """Ленивая session-factory к runtime-БД для read-запросов (FTS)."""
        if self._read_sessions_cache is None:
            engine = create_engine(self._cfg.storage.db_path.expanduser())
            self._read_sessions_cache = create_session_factory(engine)
        return self._read_sessions_cache
```

В `_build_registry`, в ветке DREAM (где регистрируется `ReadMemoryTool(mem_dir)`)
добавить:

```python
            if mem_dir is not None:
                registry.register(ReadMemoryTool(mem_dir))
                if self._cfg.memory.fts_enabled:
                    registry.register(SearchMemoryTool(self._read_session_factory()))
```

И в общей ветке, рядом с регистрацией `ReadMemoryTool` для default-профиля,
добавить тот же `if self._cfg.memory.fts_enabled: registry.register(SearchMemoryTool(...))`.

В `bridge_control.py` `_build_tools` (после `tools["read_memory"] = ...`):

```python
            if self._cfg.memory.fts_enabled:
                from svarog_harness.storage.db import create_engine, create_session_factory
                from svarog_harness.tools.memory_tools import SearchMemoryTool

                factory = create_session_factory(
                    create_engine(self._cfg.storage.db_path.expanduser())
                )
                tools["search_memory"] = SearchMemoryTool(factory)
```

> Проверь, что `BridgeControl` держит `self._cfg` и `self._memory_dir`; если
> имя конфига иное — используй фактическое (не выдумывай).

- [x] **Step 4: Запустить тесты**

Run: `uv run pytest tests/test_dream_profile.py tests/test_bridge_control.py -q`
Expected: PASS (search_memory в реестре; бридж-тесты не сломаны).

- [x] **Step 5: Коммит**

```bash
git add src/svarog_harness/runtime/run_assembly.py src/svarog_harness/runtime/bridge_control.py tests/test_dream_profile.py
git commit -m "feat(runtime): регистрация search_memory в реестре и бридже (связка B)"
```

---

### Task 7: Авто-инъекция релевантного (native, при переполнении)

`AgentLoop` получает опциональный провайдер `relevant_memory(task) -> str`;
в `run()` дописывает его результат к memory-секции. Провайдер строится в
`run_assembly.build_loop`, срабатывает только при переполнении индекса.

**Files:**
- Modify: `src/svarog_harness/runtime/loop.py` (`AgentLoop.__init__` +
  `run()`/`build_initial_messages` call)
- Modify: `src/svarog_harness/runtime/run_assembly.py` (`build_loop` строит
  провайдер, передаёт в `AgentLoop`)
- Create: `src/svarog_harness/memory/inject.py` (сборка блока)
- Test: `tests/test_memory_inject.py`

**Interfaces:**
- Consumes: `memory.index.search`, `wiki.index_overflowed`.
- Produces: `async build_relevant_block(session_factory, memory_dir, task, *,
  max_lines, pages, budget_bytes) -> str` — блок «# Релевантно задаче» или `""`
  (нет переполнения / нет совпадений / ошибка).

- [x] **Step 1: Написать падающий тест**

```python
# tests/test_memory_inject.py
"""Тесты сборки блока авто-инъекции (связка B)."""

from pathlib import Path

import pytest

from svarog_harness.memory import index as mi
from svarog_harness.memory.inject import build_relevant_block
from svarog_harness.storage.db import create_engine, create_session_factory, init_db


async def _factory(tmp_path: Path, mem: Path):
    init_db(tmp_path / "db.sqlite3")
    factory = create_session_factory(create_engine(tmp_path / "db.sqlite3"))
    async with factory() as s:
        await mi.reindex(s, mem)
    return factory


def _many_projects(mem: Path, n: int) -> None:
    for i in range(n):
        d = mem / "projects" / f"p{i}"
        d.mkdir(parents=True)
        (d / "overview.md").write_text(
            f"---\nname: P{i}\nslug: p{i}\nsummary: s{i}\nstatus: active\n---\n"
            f"тело проекта {i}\n",
            encoding="utf-8",
        )


@pytest.mark.asyncio
async def test_no_block_when_not_overflowed(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    _many_projects(mem, 2)
    factory = await _factory(tmp_path, mem)
    block = await build_relevant_block(
        factory, mem, "проекта 1", max_lines=200, pages=5, budget_bytes=3000
    )
    assert block == ""


@pytest.mark.asyncio
async def test_block_on_overflow_contains_relevant(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    _many_projects(mem, 40)
    factory = await _factory(tmp_path, mem)
    block = await build_relevant_block(
        factory, mem, "тело проекта 7", max_lines=20, pages=5, budget_bytes=3000
    )
    assert "Релевантно задаче" in block
    assert "projects/p7/overview.md" in block
    assert len(block.encode("utf-8")) <= 3000
```

- [x] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/test_memory_inject.py -q`
Expected: FAIL — `ModuleNotFoundError: ...memory.inject`.

- [x] **Step 3: Реализация**

```python
# src/svarog_harness/memory/inject.py
"""Блок авто-инъекции релевантной памяти при переполнении индекса (связка B).

При переполнении index.md хвост каталога сворачивается по дате — релевантные,
но давно не трогавшиеся страницы выпадают. Здесь FTS-ранжируем страницы против
задачи и собираем компактный блок под байтовым бюджетом. Fail-soft: нет
переполнения / нет совпадений / любая ошибка → пустая строка.
"""

import contextlib
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from svarog_harness.memory import index as memory_index
from svarog_harness.memory.wiki import index_overflowed

_HEADER = "# Релевантно задаче (по поиску в памяти)"


async def build_relevant_block(
    session_factory: async_sessionmaker,
    memory_dir: Path,
    task: str,
    *,
    max_lines: int,
    pages: int,
    budget_bytes: int,
) -> str:
    if not index_overflowed(memory_dir, max_lines=max_lines):
        return ""
    hits = []
    with contextlib.suppress(Exception):
        async with session_factory() as session:
            hits = await memory_index.search(session, task, limit=pages)
    if not hits:
        return ""
    lines = [_HEADER]
    for hit in hits:
        candidate = f"- {hit.path} — {hit.snippet}"
        if len("\n".join([*lines, candidate]).encode("utf-8")) > budget_bytes:
            break
        lines.append(candidate)
    return "\n".join(lines) if len(lines) > 1 else ""
```

В `loop.py` `AgentLoop.__init__` добавить параметр и поле (рядом с `persona`):

```python
        relevant_memory: Callable[[str], Awaitable[str]] | None = None,
        ...
        self._relevant_memory = relevant_memory
```

(`from collections.abc import Awaitable` — добавить в импорты, если нет.)

В `loop.py` в начале `run()` перед `build_initial_messages` — вычислить
эффективную память:

```python
        memory = self._memory
        if self._relevant_memory is not None:
            block = await self._relevant_memory(task)
            if block:
                memory = f"{memory}\n\n{block}" if memory else block
        messages = build_initial_messages(
            task,
            self._workspace,
            skill_cards=self._skill_cards,
            memory=memory,
            persona=self._persona,
            history=history,
        )
```

В `run_assembly.py` `build_loop` — построить провайдер и передать в `AgentLoop`:

```python
from svarog_harness.memory.inject import build_relevant_block
# ...
        relevant_memory = None
        if mem_dir is not None and cfg.memory.fts_enabled:
            factory = self._read_session_factory()

            async def relevant_memory(task: str, _mem=mem_dir, _f=factory) -> str:
                return await build_relevant_block(
                    _f,
                    _mem,
                    task,
                    max_lines=cfg.memory.index_max_lines,
                    pages=cfg.memory.fts_inject_pages,
                    budget_bytes=cfg.memory.fts_inject_bytes,
                )
```

и в конструкторе `AgentLoop(...)` добавить `relevant_memory=relevant_memory,`
рядом с `persona=persona,`.

- [x] **Step 4: Запустить тесты**

Run: `uv run pytest tests/test_memory_inject.py tests/test_loop.py -q`
Expected: PASS.

- [x] **Step 5: Коммит**

```bash
git add src/svarog_harness/memory/inject.py src/svarog_harness/runtime/loop.py src/svarog_harness/runtime/run_assembly.py tests/test_memory_inject.py
git commit -m "feat(runtime): авто-инъекция релевантной памяти при переполнении (связка B)"
```

---

### Task 8: Статус спека + финальный прогон

**Files:**
- Modify: `docs/superpowers/specs/2026-07-24-memory-fts-retrieval-design.md`
  (статус → реализовано; отметить: авто-инъекция native-only, внешние — через
  tool в бридже)

- [x] **Step 1: Обновить статус спека** на `реализовано (связка B)` и добавить
      строку в «Явно вне скоупа»: «авто-инъекция — native-only; внешние
      executor'ы получают retrieval через `search_memory` в бридже».

- [x] **Step 2: Полный прогон и линтеры**

Run: `uv run pytest -q && uv run ruff check && uv run ruff format --check && uv run mypy`
Expected: всё зелёное.

- [x] **Step 3: Коммит**

```bash
git add docs/superpowers/specs/2026-07-24-memory-fts-retrieval-design.md
git commit -m "docs(memory): статус спека FTS-retrieval — реализовано (связка B)"
```

---

## Manual verification

1. `svarog init` + наполнить память многими страницами (или взять существующий
   agent-home).
2. `svarog run "найди, где мы договаривались про версионирование API"` — агент
   вызывает `search_memory`, находит `decisions/*` по содержимому, затем
   `read_memory` полной страницы.
3. При числе страниц выше `index_max_lines` — в системном промпте (trace) виден
   блок «# Релевантно задаче» с релевантными путями.
4. `sqlite3 .svarog/svarog.db "SELECT path FROM memory_fts LIMIT 5"` — индекс
   наполнен; `user/profile.md`, `index.md`, `log.md` в нём нет.

## Self-Review

- **Покрытие спека:** MemoryIndex — Task 3; синк в writer — Task 4; tool —
  Task 5; регистрация native+бридж — Task 6; авто-инъекция — Task 7;
  overflow-сигнал — Task 2; конфиг — Task 1. Все разделы спека имеют задачу.
- **Плейсхолдеры:** код приведён в каждом шаге; проверки имён атрибутов
  (`self._cfg` в бридже) отмечены как «сверь фактическое».
- **Типы:** `SearchHit(path, snippet)`; `search(...) -> list[SearchHit]`;
  `sanitize_query(str)->str`; `index_overflowed(...)->bool`;
  `build_relevant_block(...)->str`; `SearchMemoryTool(session_factory)` —
  согласованы между задачами.
