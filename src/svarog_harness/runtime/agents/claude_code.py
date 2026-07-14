"""Адаптер Claude Code (ADR-0016 фаза 1): headless stream-json → AgentEvent.

Формат стрима (`claude -p --output-format stream-json --verbose`), по одному
JSON-объекту на строку:

* `{"type":"system","subtype":"init","session_id":…}` — старт сессии;
* `{"type":"assistant","message":{"content":[{"type":"text",…} |
  {"type":"tool_use","id":…,"name":…,"input":{…}}]}}` — ход ассистента;
* `{"type":"user","message":{"content":[{"type":"tool_result",
  "tool_use_id":…,"content":…,"is_error":…}]}}` — результат инструмента;
* `{"type":"result","subtype":"success"|"error_*","result":…,
  "total_cost_usd":…,"usage":{…},"num_turns":…}` — финал.

Неизвестные типы отдаются `opaque` с raw — дрейф формата CLI не роняет
парсер (ADR-0016 §8); контракт закрепляют golden-JSONL тесты.
"""

import json
from typing import Any

from svarog_harness.runtime.executor import AgentEvent


class ClaudeCodeAdapter:
    def __init__(self, binary: str = "claude") -> None:
        self._binary = binary

    @property
    def name(self) -> str:
        return "claude-code"

    def command(self, task: str, *, session: str | None = None) -> list[str]:
        argv = [
            self._binary,
            "-p",
            task,
            "--output-format",
            "stream-json",
            # stream-json в headless требует verbose (контракт CLI).
            "--verbose",
            # Tier 1 (ADR-0016 §6): границу держит sandbox, а не permission-
            # промпты агента; без bypass headless заблокируется на первом tool.
            "--permission-mode",
            "bypassPermissions",
        ]
        if session is not None:
            argv += ["--resume", session]
        return argv

    def parse_event(self, line: str) -> list[AgentEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            # Не-JSON строки (баннеры, прогресс) — шум, не событие.
            return []
        if not isinstance(payload, dict):
            return []
        session = _str_or_none(payload.get("session_id"))
        match payload.get("type"):
            case "assistant":
                return _parse_message_blocks(payload, session)
            case "user":
                return _parse_message_blocks(payload, session)
            case "result":
                return [_parse_result(payload, session)]
            case "system":
                # init несёт session_id — этого достаточно; остальное raw.
                return [AgentEvent(kind="opaque", session_id=session, raw=payload)]
            case _:
                return [AgentEvent(kind="opaque", session_id=session, raw=payload)]


def _parse_message_blocks(payload: dict[str, Any], session: str | None) -> list[AgentEvent]:
    message = payload.get("message")
    if not isinstance(message, dict):
        return [AgentEvent(kind="opaque", session_id=session, raw=payload)]
    content = message.get("content")
    if isinstance(content, str):
        return [AgentEvent(kind="text", text=content, session_id=session)] if content else []
    if not isinstance(content, list):
        return [AgentEvent(kind="opaque", session_id=session, raw=payload)]
    events: list[AgentEvent] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        match block.get("type"):
            case "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    events.append(AgentEvent(kind="text", text=text, session_id=session))
            case "tool_use":
                arguments = block.get("input")
                events.append(
                    AgentEvent(
                        kind="tool_call",
                        tool_name=str(block.get("name", "")),
                        call_id=_str_or_none(block.get("id")),
                        arguments=arguments if isinstance(arguments, dict) else {},
                        session_id=session,
                    )
                )
            case "tool_result":
                events.append(
                    AgentEvent(
                        kind="tool_result",
                        call_id=_str_or_none(block.get("tool_use_id")),
                        text=_result_text(block.get("content")),
                        ok=not bool(block.get("is_error", False)),
                        session_id=session,
                    )
                )
            case _:
                # thinking и будущие типы блоков — не события trace.
                continue
    return events


def _parse_result(payload: dict[str, Any], session: str | None) -> AgentEvent:
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    result = payload.get("result")
    return AgentEvent(
        kind="result",
        text=result if isinstance(result, str) else "",
        ok=payload.get("subtype") == "success" and not bool(payload.get("is_error", False)),
        session_id=session,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cost_usd=_float(payload.get("total_cost_usd")),
        num_turns=_int(payload.get("num_turns")),
        raw=payload,
    )


def _result_text(content: object) -> str:
    """tool_result.content — строка либо список text-блоков."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)
    return ""


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0
