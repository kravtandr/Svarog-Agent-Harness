"""Тесты парсера H2-секций профиля (#3)."""

from svarog_harness.memory.sections import parse_sections


def test_parse_sections_splits_h2_blocks() -> None:
    text = "# Профиль\nвступление\n\n## Тон\nкратко\n\n## Язык\nрусский\n"
    assert parse_sections(text) == {"Тон": "кратко", "Язык": "русский"}


def test_parse_sections_keeps_nested_headers_in_body() -> None:
    text = "## Предпочтения\nтекст\n### деталь\nещё\n\n## Роль\nбэкенд\n"
    result = parse_sections(text)
    assert result["Предпочтения"] == "текст\n### деталь\nещё"
    assert result["Роль"] == "бэкенд"


def test_parse_sections_empty_and_no_h2() -> None:
    assert parse_sections("") == {}
    assert parse_sections("просто текст без секций") == {}
