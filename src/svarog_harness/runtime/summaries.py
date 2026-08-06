"""Короткие сводки вызова инструмента для интерфейсов.

Строка вызова в ленте вмещает один аргумент слева и один результат справа.
Считать их должен runtime: хук `on_tool_result` зовётся из цикла, а не из
gateway, поэтому обратная зависимость невозможна.
"""

import re
from pathlib import Path
from typing import Any

_ARG_KEYS = ("path", "command", "query", "url", "name", "branch")
_ARG_LIMIT = 120

# Рабочая папка внутри контейнера (sandbox/docker.py монтирует её сюда).
_CONTAINER_WORKSPACE = "/workspace"
SANDBOX_LABEL = "<sandbox>"
# Граница по не-пути: /workspaces — другая папка, её трогать нельзя.
_CONTAINER_RE = re.compile(re.escape(_CONTAINER_WORKSPACE) + r"(?![\w.-])")


def humanize_container_path(text: str, workspace: Path | None) -> str:
    """/workspace → <sandbox>/<имя папки>: контейнерный путь человеку не адрес.

    Агент в docker видит рабочую папку как /workspace и пишет так и в
    аргументе, и в выводе (`read` по каталогу отдаёт <path>/workspace</path>).
    В ленте это читалось как «работает не в той папке» (запрос 06.08.2026).
    """
    if workspace is None or not text:
        return text
    return _CONTAINER_RE.sub(f"{SANDBOX_LABEL}/{workspace.name}", text)


def short_arg(arguments: dict[str, Any], *, workspace: Path | None = None) -> str:
    """Один осмысленный аргумент для строки вызова в ленте.

    В строке помещается ровно одно значение, поэтому берётся первый из
    известных ключей, а не сериализация всего словаря: путь и команда
    опознаются человеком с одного взгляда, `{"content": "..."}` — нет.
    """
    for key in _ARG_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            # Подставляем до обрезки: лимит считается по тому, что видно.
            shown = humanize_container_path(value, workspace)
            return shown if len(shown) <= _ARG_LIMIT else shown[: _ARG_LIMIT - 1] + "…"
    return ""


_RESULT_LIMIT = 60


def short_result(
    *, ok: bool, output: str, error: str | None = None, workspace: Path | None = None
) -> str:
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
    # Подставляем до обрезки: лимит считается по тому, что видно.
    shown = humanize_container_path(first, workspace)
    return shown if len(shown) <= _RESULT_LIMIT else shown[: _RESULT_LIMIT - 1] + "…"
