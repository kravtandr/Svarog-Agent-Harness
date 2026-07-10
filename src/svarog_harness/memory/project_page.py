"""Контракт страницы проекта в памяти-wiki (ADR-0011).

Проект живёт в `projects/<slug>/`, обязательная страница — `overview.md` с
YAML-frontmatter. Здесь — чистые функции контракта: определить, что путь это
overview проекта, провалидировать frontmatter и проставить даты. Валидацию
дёргает `remember` (ранняя ошибка модели), штамповку дат — memory-writer при
применении заявки. Автоген индекса из этих страниц — в `memory/index.py`.
"""

from datetime import date
from pathlib import PurePosixPath
from typing import Any

from svarog_harness.common.frontmatter import render, split_frontmatter

# Обязательные поля frontmatter. created/updated ведёт код (stamp_dates),
# поэтому в обязательные не входят и от модели не требуются.
REQUIRED_FIELDS = ("name", "slug", "summary", "status")
PROJECT_STATUSES = frozenset({"active", "paused", "archived"})

_OVERVIEW = "overview.md"


def project_slug_from_path(path: str) -> str | None:
    """slug, если path — это `projects/<slug>/overview.md`, иначе None."""
    parts = PurePosixPath(path).parts
    if len(parts) == 3 and parts[0] == "projects" and parts[2] == _OVERVIEW:
        return parts[1]
    return None


def validate_project_page(content: str, *, expected_slug: str) -> str | None:
    """Проверить frontmatter страницы проекта; вернуть текст ошибки или None."""
    frontmatter, _ = split_frontmatter(content)
    if not frontmatter:
        return "страница проекта должна начинаться с YAML-frontmatter (--- в начале файла)"
    missing = [f for f in REQUIRED_FIELDS if not str(frontmatter.get(f, "")).strip()]
    if missing:
        return f"во frontmatter нет обязательных полей: {', '.join(missing)}"
    status = str(frontmatter["status"]).strip().lower()
    if status not in PROJECT_STATUSES:
        allowed = ", ".join(sorted(PROJECT_STATUSES))
        return f"status='{status}' недопустим; допустимо: {allowed}"
    slug = str(frontmatter["slug"]).strip()
    if slug != expected_slug:
        return f"slug='{slug}' во frontmatter не совпадает с папкой projects/{expected_slug}/"
    return None


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def stamp_dates(content: str, *, today: date) -> str:
    """Проставить created/updated: created — сохранить, если валиден, иначе today;
    updated всегда today. Тело и порядок остальных полей сохраняются.

    Нет frontmatter → контент не трогаем (эту ошибку ловит validate_project_page).
    """
    frontmatter, body = split_frontmatter(content)
    if not frontmatter:
        return content
    frontmatter["created"] = _as_date(frontmatter.get("created")) or today
    frontmatter["updated"] = today
    return render(frontmatter, body)
