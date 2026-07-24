"""Контракт профиля пользователя и рендер персона-директивы (#3).

Профиль (`user/profile.md`) — типизированные, но необязательные H2-секции.
Поведенческие секции код превращает в директиву поведения (инструкцию в
системном промпте), фактические остаются справочным контекстом. Неизвестные
секции разрешены и в директиву не идут.
"""

from pathlib import Path

from svarog_harness.memory.sections import parse_sections

# Из этих секций собирается директива «как себя вести».
BEHAVIORAL_SECTIONS: tuple[str, ...] = ("Тон", "Язык", "Предпочтения", "Не трогать")
# Эти остаются фактами в блоке памяти, поведение из них не выводится.
FACTUAL_SECTIONS: tuple[str, ...] = ("Роль", "Расписание", "Прочее")
KNOWN_SECTIONS: tuple[str, ...] = BEHAVIORAL_SECTIONS + FACTUAL_SECTIONS

_DIRECTIVE_HEADER = "# Персонализация (следуй как инструкции)"


def render_persona_directive(profile_text: str) -> str:
    """Собрать директивный блок из непустых поведенческих секций профиля."""
    sections = parse_sections(profile_text)
    lines: list[str] = []
    for name in BEHAVIORAL_SECTIONS:
        body = sections.get(name, "").strip()
        if body:
            lines.append(f"{name}: {body}")
    if not lines:
        return ""
    return _DIRECTIVE_HEADER + "\n" + "\n".join(lines)


def load_profile(mem_dir: Path) -> str:
    """Прочитать текст `user/profile.md` (или '' если файла/каталога нет)."""
    path = mem_dir / "user" / "profile.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
