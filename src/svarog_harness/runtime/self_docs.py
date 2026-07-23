"""Self-документация Svarog для sandbox внешнего агента (ADR-0016).

Стейджит кураторскую копию доков (README, AGENTS.md, ADR) в launch-директорию
run'а и даёт агенту текст-указатель на смонтированный путь. Все три
external-адаптера читают её нативными Read/Grep — MCP reverse-tools доходят
только до Claude Code, поэтому механизм файловый, а не MCP.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_ADR_DIRNAME = "adr"


def resolve_docs_root() -> Path | None:
    """Корень репо/пакета с документацией.

    Идёт вверх от этого модуля до директории, где есть и README.md, и
    docs/adr/. None — если маркеры не найдены (нестандартная упаковка):
    вызывающий мягко отключает self-docs.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "README.md").is_file() and (parent / "docs" / _ADR_DIRNAME).is_dir():
            return parent
    return None


def _adr_title(md_path: Path) -> str:
    """Заголовок ADR из первой markdown-заголовочной строки; иначе имя файла."""
    for line in md_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return md_path.stem


def _build_index(*, has_agents: bool, adr_files: list[Path]) -> str:
    lines = ["# Svarog — документация", ""]
    lines += ["## Использование", "- README.md — команды, флаги, фичи, quick start", ""]
    if has_agents:
        lines += [
            "## Правила репозитория",
            "- AGENTS.md — как работать с кодовой базой Svarog",
            "",
        ]
    if adr_files:
        lines.append("## Архитектурные решения (ADR)")
        for adr in adr_files:
            lines.append(f"- {_ADR_DIRNAME}/{adr.name} — {_adr_title(adr)}")
        lines.append("")
    return "\n".join(lines)


def stage_self_docs(dest: Path, root: Path | None = None) -> Path | None:
    """Скопировать доки в dest и сгенерить INDEX.md.

    root=None → резолвится автоматически. None-возврат — docs-root не найден
    (фича мягко отключается). Отсутствие AGENTS.md — не ошибка.
    """
    if root is None:
        root = resolve_docs_root()
    if root is None:
        return None
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "README.md", dest / "README.md")
    has_agents = (root / "AGENTS.md").is_file()
    if has_agents:
        shutil.copy2(root / "AGENTS.md", dest / "AGENTS.md")
    adr_files = sorted((root / "docs" / _ADR_DIRNAME).glob("*.md"))
    if adr_files:
        (dest / _ADR_DIRNAME).mkdir(exist_ok=True)
        for adr in adr_files:
            shutil.copy2(adr, dest / _ADR_DIRNAME / adr.name)
    (dest / "INDEX.md").write_text(
        _build_index(has_agents=has_agents, adr_files=adr_files), encoding="utf-8"
    )
    return dest


def self_docs_hint(container_path: str) -> str:
    """Блок для контекст-файла агента: где искать доку про сам Svarog."""
    return (
        "# Документация Svarog\n\n"
        "На вопросы про сам Svarog (команды, фичи, архитектура) отвечай по "
        f"документации в {container_path}. Сначала прочитай "
        f"{container_path}/INDEX.md, затем нужный файл. Не выдумывай "
        "поведение Svarog — читай доку."
    )
