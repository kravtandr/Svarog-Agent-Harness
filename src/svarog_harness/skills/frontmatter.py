"""Парсинг YAML-frontmatter из SKILL.md (§7).

Адаптировано из HKUDS OpenHarness `skills/_frontmatter.py` (MIT; см.
docs/reference-analysis.md). Формат совместим с agentskills.io: `---`-
разделённый YAML-блок в начале файла; поля Svarog (`risk`,
`requires_approval`, `checks`) — расширение, не ломающее совместимость.

Сам разбор вынесен в нейтральный `common.frontmatter` (общий со слоем памяти,
ADR-0011); здесь — ре-экспорт для существующих импортёров скиллов.
"""

from svarog_harness.common.frontmatter import first_body_paragraph, split_frontmatter

__all__ = ["first_body_paragraph", "split_frontmatter"]
