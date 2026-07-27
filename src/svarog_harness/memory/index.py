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
    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(path, content, tokenize='unicode61')"
)


@dataclass(frozen=True)
class SearchHit:
    """Попадание поиска: путь страницы памяти и фрагмент вокруг совпадения."""

    path: str
    snippet: str


def sanitize_query(raw: str) -> str:
    """Свести запрос к списку токенов в кавычках — нейтрализует FTS5-операторы.

    FTS5 MATCH трактует OR/AND/NEAR/скобки/кавычки как синтаксис; сырой ввод
    модели их содержит и роняет запрос. Берём слова, выбрасываем кавычки,
    оборачиваем каждое — получается безопасный AND-поиск по словам.
    """
    tokens = [t for t in raw.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in tokens)


def _indexed_files(memory_dir: Path) -> list[tuple[str, str]]:
    """Пары (относительный путь, содержимое) для индексируемых страниц памяти."""
    out: list[tuple[str, str]] = []
    for md in sorted(memory_dir.rglob("*.md")):
        rel = md.relative_to(memory_dir).as_posix()
        if rel in _SKIP:
            continue
        try:
            out.append((rel, md.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return out


async def ensure_schema(session: AsyncSession) -> None:
    """Лениво создать FTS-таблицу (без Alembic: производная, перестраиваемая)."""
    await session.execute(text(_CREATE))


async def reindex(session: AsyncSession, memory_dir: Path) -> None:
    """Полный ребилд FTS-таблицы из индексируемых файлов (идемпотентен)."""
    await ensure_schema(session)
    await session.execute(text("DELETE FROM memory_fts"))
    for rel, content in _indexed_files(memory_dir):
        await session.execute(
            text("INSERT INTO memory_fts(path, content) VALUES (:p, :c)"),
            {"p": rel, "c": content},
        )
    await session.commit()


async def search(session: AsyncSession, query: str, *, limit: int) -> list[SearchHit]:
    """Top-N страниц по содержимому (bm25). Fail-soft: любая ошибка FTS → []."""
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
        # Нет таблицы / нет FTS5-расширения — retrieval недоступен, деградируем.
        return []
    return [SearchHit(path=r[0], snippet=r[1]) for r in rows]
