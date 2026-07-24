"""Тесты сборки run'а (#3): персона-директива префиксит memory-текст."""

from svarog_harness.memory.profile import render_persona_directive


def test_external_memory_text_prefixed_with_persona() -> None:
    # Фиксируем форму комбинирования директивы и блока памяти, которую
    # использует prepare_agent_launch для внешних executor'ов.
    directive = render_persona_directive("## Тон\nкратко\n")
    assert directive  # непустая — есть поведенческая секция
    memory_body = "## user/profile.md\n## Тон\nкратко"
    combined = f"{directive}\n\n{memory_body}" if directive else memory_body
    assert combined.startswith("# Персонализация")
    assert "## user/profile.md" in combined


def test_no_directive_leaves_memory_unchanged() -> None:
    directive = render_persona_directive("## Роль\nменеджер\n")  # только фактическая
    assert directive == ""
    memory_body = "## user/profile.md\nтекст"
    combined = f"{directive}\n\n{memory_body}" if directive else memory_body
    assert combined == memory_body
