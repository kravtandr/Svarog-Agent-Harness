"""Fallback-парсер tool call'ов, протёкших в текст ответа.

Некоторые серверы иногда не превращают вызов инструмента в структурированное
поле tool_calls, а отдают его обычным текстом в content. Без fallback'а loop
принимает такой ответ за финальный, вызов молча теряется, а модель успевает
отчитаться об «успехе». Поддержаны диалекты:

* Harmony (gpt-oss): ``commentary to=functions.remember json{...}``;
* Hermes/Qwen: ``<tool_call>{"name": ..., "arguments": {...}}</tool_call>``;
* Mistral: ``[TOOL_CALLS][{"name": ..., "arguments": {...}}, ...]``.

Если маркер есть, но вызов извлечь не удалось — сигналим loop'у через
leak_suspected, чтобы ответ не был принят как финальный.
"""

import json
import re
import uuid
from typing import Any

from svarog_harness.llm.provider import ToolCallRequest

# Harmony: адресация инструмента "to=functions.<имя>".
_HARMONY_MARKER = re.compile(r"to=functions[.:]([A-Za-z0-9_\-]+)")
# Между маркером и "{" допустим только служебный мусор: пробелы,
# спец-токены вида <|constrain|>/<|message|> и слова json/code.
_FILLER_TOKEN = re.compile(r"<\|[a-z_]+\|>")
_MAX_GAP_CHARS = 64

# Hermes/Qwen: JSON-объект {"name": ..., "arguments": ...} внутри тега.
_HERMES_MARKER = re.compile(r"<tool_call>", re.IGNORECASE)

# Mistral: маркер, за которым идёт JSON-массив вызовов.
_MISTRAL_MARKER = "[TOOL_CALLS]"


def leak_suspected(content: str) -> bool:
    """Похоже ли, что в тексте остался невыполненный tool call."""
    return (
        _HARMONY_MARKER.search(content) is not None
        or _HERMES_MARKER.search(content) is not None
        or _MISTRAL_MARKER in content
    )


def extract_leaked_tool_calls(content: str) -> tuple[ToolCallRequest, ...]:
    """Извлечь протёкшие tool call'ы из текста ответа.

    Возвращает только вызовы с валидным JSON-объектом аргументов: fallback
    должен срабатывать уверенно, сомнительные случаи оставляем leak_suspected.
    """
    found: list[tuple[int, str, str]] = []  # (позиция, имя, аргументы-json)
    found.extend(_extract_harmony(content))
    found.extend(_extract_hermes(content))
    found.extend(_extract_mistral(content))
    found.sort(key=lambda item: item[0])
    return tuple(
        # id уникален глобально: approval сопоставляется по call_id в рамках
        # run, и повторный id из другой итерации украл бы чужой вердикт.
        ToolCallRequest(id=f"leaked-{uuid.uuid4().hex[:8]}", name=name, arguments_json=raw)
        for _, name, raw in found
    )


def _extract_harmony(content: str) -> list[tuple[int, str, str]]:
    calls: list[tuple[int, str, str]] = []
    for match in _HARMONY_MARKER.finditer(content):
        brace = content.find("{", match.end())
        if brace == -1 or brace - match.end() > _MAX_GAP_CHARS:
            continue
        gap = _FILLER_TOKEN.sub("", content[match.end() : brace]).strip()
        if gap not in ("", "json", "code"):
            continue
        raw = _scan_balanced(content, brace, "{", "}")
        if raw is None or _load_object(raw) is None:
            continue
        calls.append((match.start(), match.group(1), raw))
    return calls


def _extract_hermes(content: str) -> list[tuple[int, str, str]]:
    calls: list[tuple[int, str, str]] = []
    for match in _HERMES_MARKER.finditer(content):
        brace = content.find("{", match.end())
        if brace == -1 or content[match.end() : brace].strip():
            continue
        raw = _scan_balanced(content, brace, "{", "}")
        if raw is None:
            continue
        entry = _call_entry(_load_object(raw))
        if entry is not None:
            calls.append((match.start(), *entry))
    return calls


def _extract_mistral(content: str) -> list[tuple[int, str, str]]:
    calls: list[tuple[int, str, str]] = []
    start = content.find(_MISTRAL_MARKER)
    while start != -1:
        bracket = content.find("[", start + len(_MISTRAL_MARKER))
        if bracket == -1 or content[start + len(_MISTRAL_MARKER) : bracket].strip():
            start = content.find(_MISTRAL_MARKER, start + len(_MISTRAL_MARKER))
            continue
        raw = _scan_balanced(content, bracket, "[", "]")
        if raw is not None:
            try:
                items = json.loads(raw)
            except json.JSONDecodeError:
                items = None
            if isinstance(items, list):
                for item in items:
                    entry = _call_entry(item if isinstance(item, dict) else None)
                    if entry is not None:
                        calls.append((start, *entry))
        start = content.find(_MISTRAL_MARKER, start + len(_MISTRAL_MARKER))
    return calls


def _call_entry(obj: dict[str, Any] | None) -> tuple[str, str] | None:
    """Превратить {"name": ..., "arguments": ...} в (имя, аргументы-json)."""
    if obj is None or not isinstance(obj.get("name"), str):
        return None
    arguments = obj.get("arguments", {})
    if isinstance(arguments, str):
        # Аргументы могут прийти уже сериализованной строкой.
        if _load_object(arguments) is None:
            return None
        return obj["name"], arguments
    if isinstance(arguments, dict):
        return obj["name"], json.dumps(arguments, ensure_ascii=False)
    return None


def _load_object(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _scan_balanced(text: str, start: int, open_ch: str, close_ch: str) -> str | None:
    """Вырезать сбалансированный JSON-фрагмент, начиная с open_ch в позиции start."""
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
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None
