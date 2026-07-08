"""Redaction известных значений секретов (ADR-0006, §12).

Вторая линия защиты: даже если команда напечатала секрет, его значение
вырезается из tool outputs и trace до записи. Основная гарантия —
невыдача секрета без явного grant'а на policy-слое.
"""

_MARKER = "[REDACTED]"


def redact(text: str, values: frozenset[str]) -> str:
    """Заменить все вхождения известных значений секретов на маркер."""
    if not text or not values:
        return text
    # Длинные значения — первыми, чтобы не оставить хвост от вложенного секрета.
    for value in sorted(values, key=len, reverse=True):
        if value and value in text:
            text = text.replace(value, _MARKER)
    return text
