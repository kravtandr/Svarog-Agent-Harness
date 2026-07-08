"""Разрешение путей из конфигурации в абсолютные (§13).

Чистые функции без побочных эффектов: используются и CLI, и оркестратором
runtime, и gateway — чтобы каталоги skills/memory считались одинаково везде.
"""

from pathlib import Path

from svarog_harness.config.schema import SvarogConfig


def memory_dir(cfg: SvarogConfig) -> Path | None:
    """Каталог memory-репозитория (Flow A), если память включена в конфиге."""
    if cfg.memory.path is None:
        return None
    return cfg.memory.path.expanduser().resolve()


def skills_dirs(cfg: SvarogConfig, workspace: Path) -> list[Path]:
    """Абсолютные пути каталогов skills из конфигурации."""
    dirs = []
    for raw in cfg.skills.paths:
        path = raw.expanduser()
        if not path.is_absolute():
            path = workspace / path
        dirs.append(path.resolve())
    return dirs


def first_existing_skills_dir(cfg: SvarogConfig, workspace: Path) -> Path | None:
    """Первый существующий каталог skills — mount ro в sandbox (ADR-0002)."""
    for path in skills_dirs(cfg, workspace):
        if path.is_dir():
            return path
    return None
