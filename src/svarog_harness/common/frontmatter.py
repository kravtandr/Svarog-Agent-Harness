"""YAML-frontmatter: разбор и сборка markdown-документов с `---`-блоком.

Нейтральный модуль: используется и скиллами (SKILL.md, §7), и памятью
(страницы проектов, ADR-0011). Формат совместим с agentskills.io — `---`-
разделённый YAML-mapping в начале файла.
"""

from typing import Any

import yaml


def split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Разделить документ на frontmatter-mapping и тело.

    Нет валидного `---`-блока → пустой frontmatter и весь текст как тело.
    Frontmatter, который не разбирается или не является mapping, тоже даёт
    пустой словарь: вызывающий сам решит, ошибка это или нет.
    """
    if not content.startswith("---\n"):
        return {}, content
    end = content.find("\n---\n", 4)
    if end == -1:
        return {}, content
    try:
        parsed = yaml.safe_load(content[4:end])
    except yaml.YAMLError:
        return {}, content[end + 5 :]
    body = content[end + 5 :]
    if not isinstance(parsed, dict):
        return {}, body
    return parsed, body


def first_body_paragraph(body: str, limit: int = 200) -> str:
    """Первый непустой абзац тела (fallback для description/summary)."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:limit]
    return ""


def render(frontmatter: dict[str, Any], body: str) -> str:
    """Собрать документ из frontmatter-mapping и тела.

    Порядок ключей сохраняется (sort_keys=False). Тело подставляется как есть,
    поэтому пустая строка после `---`, если была у автора, сохраняется.
    """
    dumped = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{dumped}\n---\n{body}"
