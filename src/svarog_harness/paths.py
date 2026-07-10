"""Безопасное соединение путей: защита от traversal вне корня (ADR-0015 §0.1).

Нейтральный util без зависимостей на config/tools — общий для файловых
операций (`file_tools`) и материализации skill-proposal (`skill_repo`), чтобы
защита от `..`/абсолютных/symlink-путей была ровно одна на всех. `resolve()`
раскрывает и `..`, и symlink'и, поэтому ссылка, ведущая наружу корня, тоже
отвергается.
"""

from pathlib import Path


class PathTraversalError(Exception):
    """Относительный путь увёл за пределы разрешённого корня."""


def safe_join(root: Path, rel: str) -> Path:
    """Разрешить `rel` строго внутри `root` или упасть с PathTraversalError.

    Запрещает абсолютные пути; раскрывает `..` и symlink через resolve();
    требует, чтобы итог лежал внутри `root` (или совпадал с ним).
    """
    candidate = Path(rel)
    if candidate.is_absolute():
        raise PathTraversalError(f"абсолютный путь запрещён: {rel}")
    root_r = root.resolve()
    resolved = (root_r / candidate).resolve()
    if resolved != root_r and not resolved.is_relative_to(root_r):
        raise PathTraversalError(f"путь выходит за пределы {root_r}: {rel}")
    return resolved


def is_within(root: Path, path: Path) -> bool:
    """True, если `path` совпадает с `root` или лежит внутри него (после resolve)."""
    root_r = root.resolve()
    path_r = path.resolve()
    return path_r == root_r or path_r.is_relative_to(root_r)
