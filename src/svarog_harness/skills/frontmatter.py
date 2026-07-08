"""Парсинг YAML-frontmatter из SKILL.md (§7).

Адаптировано из HKUDS OpenHarness `skills/_frontmatter.py` (MIT; см.
docs/reference-analysis.md). Формат совместим с agentskills.io: `---`-
разделённый YAML-блок в начале файла; поля Svarog (`risk`,
`requires_approval`, `checks`) — расширение, не ломающее совместимость.
"""

from typing import Any

import yaml


def split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Разделить SKILL.md на frontmatter-mapping и тело.

    Нет валидного `---`-блока → пустой frontmatter и весь текст как тело.
    Frontmatter, который не разбирается или не является mapping, тоже даёт
    пустой словарь: загрузчик сам решит, ошибка это или нет.
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
    """Первый непустой абзац тела (fallback для description)."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:limit]
    return ""
