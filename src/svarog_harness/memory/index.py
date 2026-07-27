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

# С какой длины токена включать префиксный поиск (короче — предлоги и союзы).
_MIN_PREFIX_LEN = 3

# Пунктуация, срезаемая перед сверкой со стоп-листом («нас,» → «нас»).
_TRIM = ".,;:!?()[]{}«»\"'—–-"

# Служебные слова выбрасываются из запроса. Иначе предлог образует совпадение
# почти с любой страницей, и в OR-проходе короткая нерелевантная страница
# обгоняет релевантную по bm25 — уверенно неверная выдача хуже пустой.
_STOPWORDS = frozenset(
    [
        "а",
        "и",
        "но",
        "или",
        "да",
        "же",
        "ли",
        "бы",
        "не",
        "ни",
        "как",
        "что",
        "чтобы",
        "если",
        "то",
        "так",
        "тут",
        "там",
        "вот",
        "в",
        "во",
        "на",
        "за",
        "из",
        "от",
        "до",
        "по",
        "с",
        "со",
        "у",
        "к",
        "ко",
        "о",
        "об",
        "обо",
        "про",
        "для",
        "при",
        "над",
        "под",
        "без",
        "я",
        "ты",
        "он",
        "она",
        "оно",
        "мы",
        "вы",
        "они",
        "мне",
        "нам",
        "нас",
        "вам",
        "вас",
        "его",
        "её",
        "их",
        "им",
        "ими",
        "себя",
        "это",
        "этот",
        "эта",
        "эти",
        "тот",
        "та",
        "те",
        "такой",
        "такие",
        "весь",
        "вся",
        "все",
        "всё",
        "есть",
        "был",
        "была",
        "были",
        "быть",
        "нет",
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "from",
        "by",
        "is",
        "are",
        "was",
        "were",
        "be",
    ]
)

_CREATE = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(path, content, tokenize='unicode61')"
)


@dataclass(frozen=True)
class SearchHit:
    """Попадание поиска: путь страницы памяти и фрагмент вокруг совпадения."""

    path: str
    snippet: str


def sanitize_query(raw: str) -> str:
    """Свести запрос к безопасному выражению FTS5 (широкий, дизъюнктивный вид).

    Используется как проверка «есть ли что искать»: пустая строка означает, что
    значимых слов в запросе нет. Сам `search` строит из тех же термов сначала
    конъюнктивное выражение и переходит к этому только при пустом результате.

    Три задачи разом:

    * **Нейтрализация синтаксиса.** MATCH трактует OR/AND/NEAR/скобки/кавычки
      как операторы; сырой ввод модели их содержит и роняет запрос. Каждый
      токен уходит в кавычки и становится литералом.
    * **Выброс служебных слов.** «где мы договаривались про версионирование» не
      должно требовать «где» и «мы» на странице — и, что важнее, не должно по
      ним совпадать: предлог образует совпадение почти со всем.
    * **Префиксный поиск.** `unicode61` не знает морфологии, поэтому «эквайринг»
      не находил «эквайрингом». Префикс закрывает наращение окончания. Смена
      основы («счета» → «счетов») ему не поддаётся — это граница лексического
      поиска, снимается только стеммингом или векторным retrieval.

    Токены короче трёх символов остаются точными: префикс на аббревиатуре («БД»)
    осмыслен, а маска по двум буквам — нет.
    """
    return " OR ".join(_terms(raw))


def _terms(raw: str) -> list[str]:
    """Значимые токены запроса, готовые к подстановке в MATCH."""
    tokens = [t for t in raw.replace('"', " ").split() if t]
    kept = [t for t in tokens if t.casefold().strip(_TRIM) not in _STOPWORDS]
    return [f'"{t}"*' if len(t) >= _MIN_PREFIX_LEN else f'"{t}"' for t in kept]


def _one_line(snippet: str) -> str:
    """Схлопнуть сниппет в одну строку.

    snippet() режет исходный текст как есть, а страницы памяти многострочные —
    перевод строки внутри фрагмента разорвал бы построчный формат выдачи
    («- путь — фрагмент») и у tool'а, и у блока авто-инъекции.
    """
    return " ".join(snippet.split())


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


_SEARCH_SQL = (
    "SELECT path, snippet(memory_fts, 1, '[', ']', '…', 12) AS snip "
    "FROM memory_fts WHERE memory_fts MATCH :q ORDER BY rank LIMIT :lim"
)


async def _match(session: AsyncSession, expr: str, limit: int) -> list[SearchHit]:
    try:
        rows = (await session.execute(text(_SEARCH_SQL), {"q": expr, "lim": limit})).all()
    except OperationalError:
        # Нет таблицы / нет FTS5-расширения — retrieval недоступен, деградируем.
        return []
    return [SearchHit(path=r[0], snippet=_one_line(r[1])) for r in rows]


async def search(session: AsyncSession, query: str, *, limit: int) -> list[SearchHit]:
    """Top-N страниц по содержимому (bm25). Fail-soft: любая ошибка FTS → [].

    Два прохода. Сначала конъюнктивный: если есть страницы со ВСЕМИ значимыми
    словами запроса — это и есть ответ, разбавлять его нечем. Только когда таких
    нет, идёт дизъюнктивный проход: он вытаскивает частичные совпадения, за счёт
    которых естественная формулировка вообще что-то находит.

    Порядок важен именно так: чистый OR выносил бы вперёд короткую страницу с
    одним общим словом, обгоняя по bm25 длинную и по-настоящему релевантную.
    """
    terms = _terms(query)
    if not terms:
        return []
    if len(terms) > 1:
        exact = await _match(session, " AND ".join(terms), limit)
        if exact:
            return exact
    return await _match(session, " OR ".join(terms), limit)
