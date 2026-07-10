"""Redaction известных значений секретов (ADR-0006, §12).

Вторая линия защиты: даже если команда напечатала секрет, его значение
вырезается из tool outputs и trace до записи. Основная гарантия —
невыдача секрета без явного grant'а на policy-слое.
"""

_MARKER = "[REDACTED]"


def redact(text: str, values: frozenset[str]) -> str:
    """Заменить известные значения и секреты узнаваемых форматов на маркер."""
    if not text:
        return text
    from svarog_harness.secrets.scanner import redact_secret_patterns

    text = redact_secret_patterns(text, _MARKER)
    if not values:
        return text
    # Длинные значения — первыми, чтобы не оставить хвост от вложенного секрета.
    for value in sorted(values, key=len, reverse=True):
        if value and value in text:
            text = text.replace(value, _MARKER)
    return text
