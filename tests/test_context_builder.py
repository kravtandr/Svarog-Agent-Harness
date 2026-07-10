"""Тесты Context Builder (§6.3): guidance по структуре памяти в системном промпте."""

from pathlib import Path

from svarog_harness.runtime.context_builder import build_initial_messages


def test_memory_section_includes_layout_guide() -> None:
    messages = build_initial_messages(
        "задача", Path("/ws"), memory="## user/profile.md\nважный факт"
    )
    system = messages[0].content
    assert "user/profile.md — факты о пользователе" in system
    assert "projects/<slug>/overview.md" in system
    assert "create перезаписывает файл целиком" in system
    assert "важный факт" in system


def test_memory_guide_documents_wiki_contract() -> None:
    system = build_initial_messages("t", Path("/ws"), memory="## index.md\nкаталог")[0].content
    # прогрессивная загрузка и автоген — ключевые правила ADR-0011
    assert "index.md" in system and "АВТОГЕН" in system
    assert "read_memory" in system
    # шаблон frontmatter страницы проекта
    assert "slug: animateyou" in system
    assert "status: active" in system


def test_without_memory_no_guide() -> None:
    messages = build_initial_messages("задача", Path("/ws"))
    assert "Долговременная память" not in messages[0].content
