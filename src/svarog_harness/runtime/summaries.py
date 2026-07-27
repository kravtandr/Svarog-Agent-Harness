"""Короткие сводки вызова инструмента для интерфейсов.

Строка вызова в ленте вмещает один аргумент слева и один результат справа.
Считать их должен runtime: хук `on_tool_result` зовётся из цикла, а не из
gateway, поэтому обратная зависимость невозможна.
"""

from typing import Any

_ARG_KEYS = ("path", "command", "query", "url", "name", "branch")
_ARG_LIMIT = 120


def short_arg(arguments: dict[str, Any]) -> str:
    """Один осмысленный аргумент для строки вызова в ленте.

    В строке помещается ровно одно значение, поэтому берётся первый из
    известных ключей, а не сериализация всего словаря: путь и команда
    опознаются человеком с одного взгляда, `{"content": "..."}` — нет.
    """
    for key in _ARG_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value if len(value) <= _ARG_LIMIT else value[: _ARG_LIMIT - 1] + "…"
    return ""


_RESULT_LIMIT = 60


def short_result(*, ok: bool, output: str, error: str | None = None) -> str:
    """Результат вызова так, как он стоит справа в строке ленты.

    Инструменты возвращают свободный текст (`ToolResult.output` — строка), а
    не измеримый итог: `write_file` пишет «записано N символов в путь»,
    `run_shell` — сырой stdout. Поэтому справа стоит первая строка вывода,
    а не «+58 −4»: числа взять неоткуда, пока инструменты не начнут
    сообщать структурированный результат.
    """
    text = (error or "ошибка") if not ok else output
    first = next((line for line in text.splitlines() if line.strip()), "").strip()
    if not first:
        return "" if ok else "ошибка"
    return first if len(first) <= _RESULT_LIMIT else first[: _RESULT_LIMIT - 1] + "…"
