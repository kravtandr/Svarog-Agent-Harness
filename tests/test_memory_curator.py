"""Тесты memory-curator: детерминированный аудит памяти-wiki (ADR-0011, шаг 5)."""

from datetime import date
from pathlib import Path

from svarog_harness.memory.curator import (
    KIND_EMPTY,
    KIND_INVALID,
    KIND_ORPHAN,
    KIND_STALE,
    audit_memory,
)

_TODAY = date(2026, 7, 10)


def _page(slug: str, *, status: str = "active", updated: str = "2026-07-10") -> str:
    return (
        f"---\nname: {slug}\nslug: {slug}\nsummary: описание\n"
        f"status: {status}\ncreated: 2026-01-01\nupdated: {updated}\n---\nтело\n"
    )


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_clean_memory_no_findings(tmp_path: Path) -> None:
    _write(tmp_path, "projects/animateyou/overview.md", _page("animateyou"))
    _write(tmp_path, "user/profile.md", "# Профиль\nфакт\n")
    report = audit_memory(tmp_path, stale_after_days=30, today=_TODAY)
    assert report.findings == []


def test_orphan_project_dir(tmp_path: Path) -> None:
    (tmp_path / "projects" / "ghost").mkdir(parents=True)
    report = audit_memory(tmp_path, stale_after_days=30, today=_TODAY)
    assert [f.kind for f in report.findings] == [KIND_ORPHAN]
    assert "ghost" in report.findings[0].path


def test_invalid_frontmatter(tmp_path: Path) -> None:
    _write(tmp_path, "projects/x/overview.md", "# нет frontmatter\n")
    report = audit_memory(tmp_path, stale_after_days=30, today=_TODAY)
    assert any(f.kind == KIND_INVALID for f in report.findings)


def test_slug_mismatch_is_invalid(tmp_path: Path) -> None:
    _write(tmp_path, "projects/x/overview.md", _page("other"))
    report = audit_memory(tmp_path, stale_after_days=30, today=_TODAY)
    kinds = [f.kind for f in report.findings]
    assert KIND_INVALID in kinds


def test_stale_active_page(tmp_path: Path) -> None:
    _write(tmp_path, "projects/old/overview.md", _page("old", updated="2026-01-01"))
    report = audit_memory(tmp_path, stale_after_days=30, today=_TODAY)
    stale = [f for f in report.findings if f.kind == KIND_STALE]
    assert len(stale) == 1
    assert "projects/old/overview.md" in stale[0].path


def test_archived_old_page_not_stale(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "projects/old/overview.md",
        _page("old", status="archived", updated="2026-01-01"),
    )
    report = audit_memory(tmp_path, stale_after_days=30, today=_TODAY)
    assert all(f.kind != KIND_STALE for f in report.findings)


def test_recent_active_not_stale(tmp_path: Path) -> None:
    _write(tmp_path, "projects/fresh/overview.md", _page("fresh", updated="2026-07-01"))
    report = audit_memory(tmp_path, stale_after_days=30, today=_TODAY)
    assert report.findings == []


def test_empty_file_flagged_autogen_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "decisions/blank.md", "   \n")
    _write(tmp_path, "index.md", "")  # автоген — не флагается
    _write(tmp_path, "log.md", "")
    report = audit_memory(tmp_path, stale_after_days=30, today=_TODAY)
    empties = [f for f in report.findings if f.kind == KIND_EMPTY]
    assert [f.path for f in empties] == ["decisions/blank.md"]


def test_report_markdown(tmp_path: Path) -> None:
    (tmp_path / "projects" / "ghost").mkdir(parents=True)
    md = audit_memory(tmp_path, stale_after_days=30, today=_TODAY).to_markdown()
    assert "Memory curation report" in md
    assert "orphan" in md
