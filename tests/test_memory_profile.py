"""Тесты контракта профиля и персона-директивы (#3)."""

from pathlib import Path

from svarog_harness.memory.profile import (
    BEHAVIORAL_SECTIONS,
    load_profile,
    render_persona_directive,
)


def test_directive_uses_only_behavioral_sections() -> None:
    text = (
        "## Тон\nкратко, без воды\n\n"
        "## Язык\nрусский\n\n"
        "## Роль\nбэкенд в Северстали\n"  # фактическая — не в директиве
    )
    directive = render_persona_directive(text)
    assert "кратко, без воды" in directive
    assert "русский" in directive
    assert "Северстали" not in directive
    assert "Тон:" in directive and "Язык:" in directive


def test_directive_empty_when_no_behavioral_sections() -> None:
    assert render_persona_directive("") == ""
    assert render_persona_directive("## Роль\nменеджер\n") == ""


def test_load_profile_reads_file_or_empty(tmp_path: Path) -> None:
    assert load_profile(tmp_path) == ""
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "profile.md").write_text("## Тон\nживо\n", encoding="utf-8")
    assert "живо" in load_profile(tmp_path)


def test_behavioral_sections_are_domain_neutral() -> None:
    assert "Стиль кода" not in BEHAVIORAL_SECTIONS
    assert "Предпочтения" in BEHAVIORAL_SECTIONS
