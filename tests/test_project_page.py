"""Тесты контракта страницы проекта в памяти-wiki (ADR-0011)."""

from datetime import date

from svarog_harness.common.frontmatter import split_frontmatter
from svarog_harness.memory.project_page import (
    project_slug_from_path,
    stamp_dates,
    validate_project_page,
)

_VALID = """\
---
name: AnimateYou
slug: animateyou
summary: бот генерации медиа
status: active
tags: [bot, media]
---
Описание проекта.
"""


def test_slug_from_path() -> None:
    assert project_slug_from_path("projects/animateyou/overview.md") == "animateyou"
    assert project_slug_from_path("projects/animateyou/notes.md") is None
    assert project_slug_from_path("user/profile.md") is None
    assert project_slug_from_path("projects/overview.md") is None


def test_validate_ok() -> None:
    assert validate_project_page(_VALID, expected_slug="animateyou") is None


def test_validate_missing_frontmatter() -> None:
    err = validate_project_page("просто текст", expected_slug="x")
    assert err is not None and "frontmatter" in err


def test_validate_missing_required_field() -> None:
    content = "---\nname: X\nslug: x\nstatus: active\n---\n"  # нет summary
    err = validate_project_page(content, expected_slug="x")
    assert err is not None and "summary" in err


def test_validate_bad_status() -> None:
    content = "---\nname: X\nslug: x\nsummary: y\nstatus: wip\n---\n"
    err = validate_project_page(content, expected_slug="x")
    assert err is not None and "status" in err


def test_validate_slug_mismatch() -> None:
    err = validate_project_page(_VALID, expected_slug="other")
    assert err is not None and "slug" in err


def test_stamp_dates_sets_both_on_first_write() -> None:
    today = date(2026, 7, 10)
    stamped = stamp_dates(_VALID, today=today)
    fm, body = split_frontmatter(stamped)
    assert str(fm["created"]) == "2026-07-10"
    assert str(fm["updated"]) == "2026-07-10"
    assert "Описание проекта." in body  # тело сохранено


def test_stamp_dates_preserves_created_updates_updated() -> None:
    first = stamp_dates(_VALID, today=date(2026, 1, 1))
    later = stamp_dates(first, today=date(2026, 7, 10))
    fm, _ = split_frontmatter(later)
    assert str(fm["created"]) == "2026-01-01"
    assert str(fm["updated"]) == "2026-07-10"


def test_stamp_dates_noop_without_frontmatter() -> None:
    assert stamp_dates("нет frontmatter", today=date(2026, 7, 10)) == "нет frontmatter"
