"""Тесты Context Builder (§6.3): guidance по структуре памяти в системном промпте."""

from pathlib import Path

from svarog_harness.runtime.context_builder import build_initial_messages


def test_memory_section_includes_layout_guide() -> None:
    messages = build_initial_messages(
        "задача", Path("/ws"), memory="## user/profile.md\nважный факт"
    )
    system = messages[0].content
    assert "user/profile.md — профиль пользователя типизированными H2-секциями" in system
    assert "projects/<slug>/overview.md" in system
    assert "create перезаписывает файл целиком" in system
    assert "важный факт" in system


def test_persona_directive_injected_as_instruction() -> None:
    messages = build_initial_messages(
        "задача",
        Path("/ws"),
        memory="## user/profile.md\n...",
        persona="# Персонализация (следуй как инструкции)\nТон: кратко",
    )
    system = messages[0].content
    assert messages[0].role == "system"
    assert "Персонализация (следуй как инструкции)" in system
    assert "Тон: кратко" in system


def test_persona_absent_when_empty() -> None:
    messages = build_initial_messages("задача", Path("/ws"), persona="")
    assert "Персонализация" not in messages[0].content


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


def test_system_prompt_includes_competency_honesty_rule() -> None:
    """S26: native-промпт несёт правило честности про пробелы в компетенциях —
    иначе модель назначает ближайшего стек-соседа без оговорки (React → mobile)."""
    system = build_initial_messages("t", Path("/ws"), memory="")[0].content
    assert "пробелы в компетенциях" in system.lower()
    assert "React ≠ mobile" in system
