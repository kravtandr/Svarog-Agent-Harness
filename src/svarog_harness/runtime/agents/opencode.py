"""Адаптер OpenCode (ADR-0016 фаза 4): `opencode run --format json` → AgentEvent.

Формат стрима (JSONL): события `step_start`, `text`, `reasoning`,
`tool_use`, `step_finish`, `error`; каждое несёт `sessionID`
(формат `ses_…`) и `part` с деталями. `tool_use` приходит уже завершённым
(part.state: status/input/output), поэтому адаптер разворачивает его в пару
tool_call + tool_result. `step_finish` с reason="stop" — финал шага с
usage/cost. Resume — `--session <id>`.
"""

import json
from pathlib import PurePosixPath
from typing import Any

from svarog_harness.runtime.executor import AdapterCapabilities, AgentEvent, AgentLaunch

# Домашний каталог контейнера (docker.py: HOME=/tmp/home): OpenCode держит
# конфиг в ~/.config/opencode, состояние в ~/.local/share/opencode —
# персистентным делаем весь home.
_STATE_DIR = PurePosixPath("/tmp/home")


class OpencodeAdapter:
    def __init__(self, binary: str = "opencode") -> None:
        self._binary = binary

    @property
    def name(self) -> str:
        return "opencode"

    @property
    def wire_format(self) -> str:
        return "openai"

    def capabilities(self) -> AdapterCapabilities:
        # hooks/mcp: permission-хуков нет; MCP-конфиг OpenCode не совместим
        # с HTTP-bridge Svarog — честно False (supervised → fail-closed).
        return AdapterCapabilities(hooks=False, resume=True, mcp=False)

    def command(self, launch: AgentLaunch) -> list[str]:
        argv = [self._binary, "run", launch.task, "--format", "json"]
        if launch.session is not None:
            argv += ["--session", launch.session]
        return argv

    def base_url_env(self, base_url: str, api_key: str) -> dict[str, str]:
        # Провайдер задаётся конфигом OpenCode в образе/стейте; для
        # OpenAI-совместимого провайдера направляем на bridge (§3).
        return {
            "OPENAI_BASE_URL": base_url + "/v1",
            "OPENAI_API_KEY": api_key,
        }

    def state_dir(self) -> PurePosixPath:
        return _STATE_DIR

    def context_files(self, memory: str, skill_cards: str) -> dict[str, str]:
        """Глобальные правила OpenCode: ~/.config/opencode/AGENTS.md."""
        sections: list[str] = []
        if memory:
            sections.append(f"# Память Svarog\n\n{memory}")
        if skill_cards:
            sections.append(f"# Скиллы Svarog\n\n{skill_cards}")
        if not sections:
            return {}
        return {".config/opencode/AGENTS.md": "\n\n".join(sections) + "\n"}

    def managed_policy(self, mcp_config: str | None, hook_command: str | None) -> str | None:
        return None

    def managed_policy_path(self) -> PurePosixPath | None:
        return None

    def parse_event(self, line: str) -> list[AgentEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        session = _str_or_none(payload.get("sessionID"))
        part = payload.get("part")
        part = part if isinstance(part, dict) else {}
        match payload.get("type"):
            case "text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    return [AgentEvent(kind="text", text=text, session_id=session)]
                return []
            case "tool_use":
                return self._parse_tool_use(part, session)
            case "step_finish":
                if payload.get("reason") != "stop" and part.get("reason") != "stop":
                    return []  # промежуточный шаг (tool-calls) — не финал
                tokens = payload.get("tokens") or part.get("tokens")
                tokens = tokens if isinstance(tokens, dict) else {}
                cost = payload.get("cost", part.get("cost"))
                return [
                    AgentEvent(
                        kind="result",
                        ok=True,
                        session_id=session,
                        input_tokens=_int(tokens.get("input")),
                        output_tokens=_int(tokens.get("output")),
                        cost_usd=float(cost) if isinstance(cost, int | float) else 0.0,
                        raw=payload,
                    )
                ]
            case "error":
                error = payload.get("error")
                message = ""
                if isinstance(error, dict):
                    data = error.get("data")
                    message = str(
                        (data or {}).get("message", "") if isinstance(data, dict) else ""
                    ) or str(error.get("name", ""))
                return [
                    AgentEvent(
                        kind="result", ok=False, text=message, session_id=session, raw=payload
                    )
                ]
            case "step_start":
                return [AgentEvent(kind="opaque", session_id=session, raw=payload)]
            case "reasoning":
                return []  # thinking — не событие trace
            case _:
                return [AgentEvent(kind="opaque", session_id=session, raw=payload)]

    def _parse_tool_use(self, part: dict[str, Any], session: str | None) -> list[AgentEvent]:
        state = part.get("state")
        state = state if isinstance(state, dict) else {}
        call_id = _str_or_none(part.get("id"))
        arguments = state.get("input")
        output = state.get("output")
        return [
            AgentEvent(
                kind="tool_call",
                tool_name=str(part.get("tool", "")),
                call_id=call_id,
                arguments=arguments if isinstance(arguments, dict) else {},
                session_id=session,
            ),
            AgentEvent(
                kind="tool_result",
                call_id=call_id,
                text=output
                if isinstance(output, str)
                else json.dumps(output, ensure_ascii=False)
                if output is not None
                else "",
                ok=state.get("status") in (None, "completed"),
                session_id=session,
            ),
        ]


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
