"""Чтение памяти в контекст (§6.7, ADR-0011): только «горячие» файлы.

В контекст подаётся не весь memory-репозиторий, а компактный набор:
навигационный index.md и профиль пользователя. Страницы проектов и decisions/
агент подгружает по требованию через read_memory (progressive disclosure) —
так контекст не раздувается и файлы не выпадают из-за усечения по лимиту.
"""

from pathlib import Path

_DEFAULT_LIMIT_BYTES = 16_000

# Порядок важен: профиль пользователя первым (самое ценное; при усечении
# режется хвост), затем навигационный индекс. Оба малы по замыслу.
_HOT_FILES = ("user/profile.md", "index.md")


def read_memory(memory_dir: Path, *, limit_bytes: int = _DEFAULT_LIMIT_BYTES) -> str:
    """Собрать «горячие» файлы памяти в один блок с заголовками-путями.

    Остальная память видна агенту через index.md и подгружается read_memory.
    """
    if not memory_dir.is_dir():
        return ""
    parts: list[str] = []
    total = 0
    for rel in _HOT_FILES:
        md = memory_dir / rel
        if not md.is_file():
            continue
        try:
            text = md.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        block = f"## {rel}\n{text}"
        total += len(block.encode("utf-8"))
        if total > limit_bytes:
            parts.append(f"## {rel}\n[память усечена: превышен лимит {limit_bytes} байт]")
            break
        parts.append(block)
    return "\n\n".join(parts)
