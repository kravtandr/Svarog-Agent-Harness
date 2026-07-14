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
from pathlib import PurePosixPath
from typing import Any

from svarog_harness.runtime.executor import (
    AdapterCapabilities,
    AgentAuth,
    AgentEvent,
    AgentLaunch,
)

# HOME в sandbox-контейнере задан явно (docker.py: -e HOME=/tmp/home).
_STATE_DIR = PurePosixPath("/tmp/home/.claude")
# Managed-настройки читаются с высшим приоритетом и не переопределяются
# project-слоем (.claude/settings.json в workspace) — ADR-0016 §6.
_MANAGED_SETTINGS = PurePosixPath("/etc/claude-code/managed-settings.json")


class ClaudeCodeAdapter:
    def __init__(self, binary: str = "claude") -> None:
        self._binary = binary

    @property
    def name(self) -> str:
        return "claude-code"

    @property
    def wire_format(self) -> str:
        return "anthropic"

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(hooks=True, resume=True, mcp=True)

    def command(self, launch: AgentLaunch) -> list[str]:
        argv = [
            self._binary,
            "-p",
            launch.task,
            "--output-format",
            "stream-json",
            # stream-json в headless требует verbose (контракт CLI).
            "--verbose",
            # Tier 1 (ADR-0016 §6): границу держит sandbox, а не permission-
            # промпты агента; без bypass headless заблокируется на первом tool.
            "--permission-mode",
            "bypassPermissions",
        ]
        if launch.mcp_config is not None:
            # strict: только MCP-серверы Svarog, чужие конфиги workspace
            # игнорируются (агент не может подсунуть свой сервер).
            argv += ["--mcp-config", launch.mcp_config, "--strict-mcp-config"]
        if launch.session is not None:
            argv += ["--resume", launch.session]
        return argv

    def base_url_env(self, auth: AgentAuth) -> dict[str, str]:
        env = {
            "ANTHROPIC_BASE_URL": auth.base_url,
            # Телеметрия/автообновления не пройдут через egress-периметр —
            # выключаем, чтобы агент не тратил время на ретраи.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
        }
        if auth.mode == "subscription":
            # OAuth-токен подписки (claude setup-token). ANTHROPIC_API_KEY НЕ
            # ставим: по precedence он перебил бы OAuth-токен (docs §auth).
            env["CLAUDE_CODE_OAUTH_TOKEN"] = auth.credential
        else:
            # per-run токен bridge; настоящий ключ инжектирует прокси (§3).
            env["ANTHROPIC_API_KEY"] = auth.proxy_token
        return env

    def state_dir(self) -> PurePosixPath:
        return _STATE_DIR

    def context_files(self, memory: str, skill_cards: str) -> dict[str, str]:
        """~/.claude/CLAUDE.md — глобальная память агента: контекст Svarog
        не попадает в workspace и не коммитится git-flow (ADR-0016 §4)."""
        sections: list[str] = []
        if memory:
            sections.append(f"# Память Svarog\n\n{memory}")
        if skill_cards:
            sections.append(
                "# Скиллы Svarog\n\nПолное содержимое скилла — MCP-tool `read_skill`.\n\n"
                + skill_cards
            )
        if not sections:
            return {}
        return {"CLAUDE.md": "\n\n".join(sections) + "\n"}

    def managed_policy(self, mcp_config: str | None, hook_command: str | None) -> str | None:
        settings: dict[str, Any] = {"permissions": {"defaultMode": "bypassPermissions"}}
        if hook_command is not None:
            # PreToolUse → policy-мост Svarog (tier 2, ADR-0016 §6): matcher *
            # — каждый вызов инструмента проходит через bridge /hook.
            settings["hooks"] = {
                "PreToolUse": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": hook_command}]}
                ]
            }
        return json.dumps(settings, ensure_ascii=False, indent=2)

    def managed_policy_path(self) -> PurePosixPath | None:
        return _MANAGED_SETTINGS

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
