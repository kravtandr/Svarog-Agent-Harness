"""Self-документация Svarog: каталог и чтение доков репо (ADR-0016).

Отдаётся внешнему агенту reverse-tool'ом `read_svarog_docs` через bridge, а
НЕ файлами в контейнере: `read` OpenCode жёстко отвергает любой путь вне cwd
(«filePath resolves outside the working directory»), поэтому ro-mount доков
вне /workspace для него нечитаем, а класть их в workspace нельзя — их подберёт
git-flow. MCP есть у claude-code и opencode (матрица capabilities §1), то есть
у обоих развёртываемых executor'ов; codex (mcp=False) указателя не получает.

Пространство имён документов плоское и задано allowlist'ом — `README.md`,
`AGENTS.md`, `adr/<файл>.md`. Обхода вне корня не существует по построению:
произвольные пути не резолвятся, а отвергаются.
"""

from __future__ import annotations

from pathlib import Path

_ADR_PREFIX = "adr/"
_TOP_LEVEL = ("README.md", "AGENTS.md")


def resolve_docs_root() -> Path | None:
    """Корень репо/пакета с документацией.

    Идёт вверх от этого модуля до директории, где есть и README.md, и
    docs/adr/. None — если маркеры не найдены (нестандартная упаковка):
    вызывающий мягко отключает self-docs.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "README.md").is_file() and (parent / "docs" / "adr").is_dir():
            return parent
    return None


def _doc_path(rel: str, root: Path) -> Path:
    """Виртуальный путь документа → реальный файл. ValueError — вне allowlist."""
    name = rel.strip().lstrip("/")
    if name in _TOP_LEVEL:
        return root / name
    if name.startswith(_ADR_PREFIX):
        leaf = name[len(_ADR_PREFIX) :]
        if "/" not in leaf and leaf.endswith(".md") and not leaf.startswith("."):
            return root / "docs" / "adr" / leaf
    raise ValueError(
        f"неизвестный документ '{rel}'; доступны README.md, AGENTS.md и adr/<файл>.md "
        "(полный список — вызов без параметра path)"
    )


def _adr_title(md_path: Path) -> str:
    """Заголовок ADR из первой markdown-заголовочной строки; иначе имя файла."""
    for line in md_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return md_path.stem


def build_docs_index(root: Path | None = None) -> str:
    """Каталог доступных документов — что агент видит первым вызовом."""
    if root is None:
        root = resolve_docs_root()
    if root is None:
        return ""
    # Записи ниже — НЕ файлы на диске: наблюдение 23.07.2026, модель брала
    # путь из каталога и звала нативный read по /workspace/<путь>, промахиваясь.
    lines = [
        "# Документация Svarog",
        "",
        "Ниже — идентификаторы документов, а НЕ пути к файлам на диске. Их нет "
        "в workspace: нативные read/glob/grep по ним не сработают. Чтобы "
        "прочитать документ, вызови этот же tool ещё раз с параметром "
        '`path`, равным идентификатору (например `path="adr/0003-...md"`).',
        "",
    ]
    lines += ["## Использование", "- README.md — команды, флаги, фичи, quick start", ""]
    if (root / "AGENTS.md").is_file():
        lines += [
            "## Правила репозитория",
            "- AGENTS.md — как работать с кодовой базой Svarog",
            "",
        ]
    adr_files = sorted((root / "docs" / "adr").glob("*.md"))
    if adr_files:
        lines.append("## Архитектурные решения (ADR)")
        for adr in adr_files:
            lines.append(f"- {_ADR_PREFIX}{adr.name} — {_adr_title(adr)}")
        lines.append("")
    return "\n".join(lines)


def read_doc(rel: str, root: Path | None = None) -> str:
    """Содержимое документа по виртуальному пути.

    ValueError — путь вне allowlist или файла нет.
    """
    if root is None:
        root = resolve_docs_root()
    if root is None:
        raise ValueError("документация Svarog недоступна")
    path = _doc_path(rel, root)
    if not path.is_file():
        raise ValueError(f"документа '{rel}' нет")
    return path.read_text(encoding="utf-8")


def self_docs_hint(tool_name: str) -> str:
    """Блок для контекст-файла агента: как узнать правду про сам Svarog."""
    return (
        "# Документация Svarog\n\n"
        f"Про сам Svarog (команды, фичи, архитектура, ADR) отвечай ТОЛЬКО по "
        f"документации: вызови MCP-tool `{tool_name}` без параметров — получишь "
        f"каталог, затем вызови его с `path` нужного файла.\n\n"
        "Документации Svarog НЕТ в workspace: glob/grep/read по рабочему каталогу "
        f"её не найдут, искать файлы бесполезно — только `{tool_name}`.\n\n"
        "Твои общие знания о «свароге» неприменимы: это конкретная система, её "
        "поведение описано только в этих документах. Не отвечай по памяти и не "
        "угадывай — сначала прочитай доку."
    )
