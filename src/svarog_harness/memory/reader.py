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
        size = len(block.encode("utf-8"))
        if total + size > limit_bytes:
            # Страховка при чтении (ADR-0015 §1.5): усечение по границе строки
            # и warning с действием, а не молчаливая заглушка. Основной потолок
            # обеспечивается при генерации индекса (wiki.render_index).
            head = _clip_to_bytes(text, max(0, limit_bytes - total - len(rel) - 16))
            warning = (
                f"> WARNING: {rel} превысил лимит контекста ({limit_bytes} байт) — "
                f"загружена часть. Сокращай summary; детали переноси в notes.md"
            )
            parts.append(f"## {rel}\n{head}\n{warning}" if head else f"## {rel}\n{warning}")
            break
        total += size
        parts.append(block)
    return "\n\n".join(parts)


def _clip_to_bytes(text: str, budget: int) -> str:
    """Голова текста по границе строки, укладывающаяся в budget байт."""
    kept: list[str] = []
    size = 0
    for line in text.splitlines():
        size += len(line.encode("utf-8")) + 1
        if size > budget:
            break
        kept.append(line)
    return "\n".join(kept)
