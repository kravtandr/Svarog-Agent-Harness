"""Парсер markdown на H2-секции (#3, гиперперсонализация профиля).

Возвращает {заголовок H2: тело}. Тело — всё до следующего H2 (или EOF),
включая вложенные H3+. Текст до первого H2 отбрасывается. Чистая функция,
без IO — рядом с `memory/apply.py`, но общего назначения.
"""


def parse_sections(text: str) -> dict[str, str]:
    lines = text.splitlines()
    sections: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []

    def flush() -> None:
        if current is not None:
            sections[current] = "\n".join(body).strip()

    for line in lines:
        stripped = line.strip()
        is_h2 = stripped.startswith("## ") and not stripped.startswith("### ")
        if is_h2:
            flush()
            current = stripped[3:].strip()
            body = []
        elif current is not None:
            body.append(line)
    flush()
    return sections
