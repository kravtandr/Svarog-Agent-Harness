"""Тесты Context Builder (§6.3): guidance по структуре памяти в системном промпте."""

from pathlib import Path

from svarog_harness.runtime.context_builder import build_initial_messages


def test_memory_section_includes_layout_guide() -> None:
    messages = build_initial_messages(
        "задача", Path("/ws"), memory="## user/profile.md\nважный факт"
    )
    system = messages[0].content
    assert "user/profile.md — факты о пользователе" in system
    assert "projects/<имя-проекта>.md" in system
    assert "create перезаписывает файл целиком" in system
    assert "важный факт" in system


def test_without_memory_no_guide() -> None:
    messages = build_initial_messages("задача", Path("/ws"))
    assert "Долговременная память" not in messages[0].content
