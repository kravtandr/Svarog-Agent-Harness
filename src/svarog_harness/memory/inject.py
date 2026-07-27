"""Блок авто-инъекции релевантной памяти при переполнении индекса (связка B).

При переполнении index.md хвост каталога сворачивается по дате — релевантные,
но давно не трогавшиеся страницы выпадают. Здесь FTS-ранжируем страницы против
задачи и собираем компактный блок под байтовым бюджетом. Fail-soft: нет
переполнения / нет совпадений / любая ошибка → пустая строка.
"""

import contextlib
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from svarog_harness.memory import index as memory_index
from svarog_harness.memory.wiki import index_overflowed

_HEADER = "# Релевантно задаче (по поиску в памяти)"


async def build_relevant_block(
    session_factory: async_sessionmaker[AsyncSession],
    memory_dir: Path,
    task: str,
    *,
    max_lines: int,
    pages: int,
    budget_bytes: int,
) -> str:
    """Блок «Релевантно задаче» с top-K страницами или пустая строка.

    Пустая строка означает «ничего не добавляем»: индекс не переполнен (тогда
    навигации по index.md достаточно), совпадений нет или FTS недоступен.
    """
    if not index_overflowed(memory_dir, max_lines=max_lines):
        return ""
    hits: list[memory_index.SearchHit] = []
    # Retrieval — улучшение контекста, а не условие работы: любая ошибка БД
    # оставляет промпт таким, каким он был бы без связки B.
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
