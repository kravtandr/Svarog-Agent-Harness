"""Fallback-парсер tool call'ов, протёкших в текст ответа (Harmony-формат).

Некоторые серверы (gpt-oss и др.) иногда не превращают вызов инструмента
в структурированное поле tool_calls, а отдают его обычным текстом в content:
``...commentary to=functions.remember json{...}...``. Без fallback'а loop
принимает такой ответ за финальный, вызов молча теряется, а модель успевает
отчитаться об «успехе». Здесь мы вытаскиваем такие вызовы из текста; если
маркер есть, но JSON извлечь не удалось — сигналим loop'у через leak_suspected.
"""

import json
import re

from svarog_harness.llm.provider import ToolCallRequest

# Маркер адресации инструмента в Harmony: "to=functions.<имя>".
_CALL_MARKER = re.compile(r"to=functions[.:]([A-Za-z0-9_\-]+)")
# Между маркером и "{" допустим только служебный мусор: пробелы,
# спец-токены вида <|constrain|>/<|message|> и слова json/code.
_FILLER_TOKEN = re.compile(r"<\|[a-z_]+\|>")
_MAX_GAP_CHARS = 64


def leak_suspected(content: str) -> bool:
    """Похоже ли, что в тексте остался невыполненный tool call."""
    return _CALL_MARKER.search(content) is not None


def extract_leaked_tool_calls(content: str) -> tuple[ToolCallRequest, ...]:
    """Извлечь протёкшие tool call'ы из текста ответа.

    Возвращает только вызовы с валидным JSON-объектом аргументов: fallback
    должен срабатывать уверенно, сомнительные случаи оставляем leak_suspected.
    """
    calls: list[ToolCallRequest] = []
    for match in _CALL_MARKER.finditer(content):
        name = match.group(1)
        brace = content.find("{", match.end())
        if brace == -1 or brace - match.end() > _MAX_GAP_CHARS:
            continue
        gap = _FILLER_TOKEN.sub("", content[match.end() : brace]).strip()
        if gap not in ("", "json", "code"):
            continue
        raw = _scan_json_object(content, brace)
        if raw is None:
            continue
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(arguments, dict):
            continue
        calls.append(ToolCallRequest(id=f"leaked-{len(calls) + 1}", name=name, arguments_json=raw))
    return tuple(calls)


def _scan_json_object(text: str, start: int) -> str | None:
    """Вырезать сбалансированный JSON-объект, начиная с '{' в позиции start."""
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None
