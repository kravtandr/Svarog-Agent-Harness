"""Чтение памяти в контекст (§6.7): конкатенация memory/**/*.md с лимитом.

Чтение — без ограничений, напрямую из working tree (ADR-0004). Лимит
memory-entrypoint (§6.7) не даёт памяти бесконтрольно раздувать промпт.
"""

from pathlib import Path

_DEFAULT_LIMIT_BYTES = 16_000

# Порядок включения в контекст: усечение по лимиту режет хвост, поэтому
# самое ценное (профиль пользователя) идёт первым, а не по алфавиту,
# где user/ оказался бы последним и выпадал бы из контекста первым.
_DIR_PRIORITY = {"user": 0, "projects": 1, "decisions": 2}


def _priority(memory_dir: Path, md: Path) -> tuple[int, str]:
    rel = md.relative_to(memory_dir)
    return _DIR_PRIORITY.get(rel.parts[0], len(_DIR_PRIORITY)), str(rel)


def read_memory(memory_dir: Path, *, limit_bytes: int = _DEFAULT_LIMIT_BYTES) -> str:
    """Собрать все memory/**/*.md в один блок с заголовками-путями, усечь по лимиту."""
    if not memory_dir.is_dir():
        return ""
    parts: list[str] = []
    total = 0
    for md in sorted(memory_dir.rglob("*.md"), key=lambda p: _priority(memory_dir, p)):
        try:
            text = md.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        rel = md.relative_to(memory_dir)
        block = f"## {rel}\n{text}"
        total += len(block.encode("utf-8"))
        if total > limit_bytes:
            parts.append(f"## {rel}\n[память усечена: превышен лимит {limit_bytes} байт]")
            break
        parts.append(block)
    return "\n\n".join(parts)
