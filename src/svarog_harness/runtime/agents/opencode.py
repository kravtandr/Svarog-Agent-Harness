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

from svarog_harness.runtime.executor import (
    AdapterCapabilities,
    AgentAuth,
    AgentEvent,
    AgentLaunch,
    ask_user_guide,
)

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
        # hooks: permission-хуков нет — supervised остаётся fail-closed.
        # mcp: мост Svarog подключается remote-секцией managed-конфига
        # (спайк 2026-07-21, spec 2026-07-21-sim-blockers-fix §1a).
        return AdapterCapabilities(hooks=False, resume=True, mcp=True)

    def command(self, launch: AgentLaunch) -> list[str]:
        argv = [self._binary, "run", launch.task, "--format", "json"]
        if launch.session is not None:
            argv += ["--session", launch.session]
        return argv

    def base_url_env(self, auth: AgentAuth) -> dict[str, str]:
        # Провайдер задаётся конфигом OpenCode в образе/стейте; для
        # OpenAI-совместимого провайдера направляем на bridge (§3).
        # subscription не поддержан — только api-key (валидатор конфига).
        return {
            "OPENAI_BASE_URL": auth.base_url + "/v1",
            "OPENAI_API_KEY": auth.proxy_token,
        }

    def state_dir(self) -> PurePosixPath:
        return _STATE_DIR

    def context_files(self, memory: str, skill_cards: str) -> dict[str, str]:
        """Глобальные правила OpenCode: ~/.config/opencode/AGENTS.md."""
        sections: list[str] = []
        # Имена tools глазами OpenCode — с префиксом svarog_ (спайк 2026-07-21).
        sections.append(
            "# Память\n\n"
            "Единственный источник истины по памяти — Svarog. Чтобы что-то "
            "запомнить между запусками, вызывай MCP-tool `svarog_remember` "
            "(прочитать — `svarog_read_memory`); НЕ пиши факты в файлы "
            "workspace и НЕ веди свою локальную память в ~/.local/share/opencode."
            + (f"\n\nТекущая память Svarog:\n\n{memory}" if memory else "")
        )
        sections.append(ask_user_guide("svarog_ask_user"))
        if skill_cards:
            sections.append(f"# Скиллы Svarog\n\n{skill_cards}")
        return {".config/opencode/AGENTS.md": "\n\n".join(sections) + "\n"}

    def provider_files(self, model: str | None) -> dict[str, str]:
        """Managed-конфиг провайдера: ~/.config/opencode/opencode.jsonc.

        Без него OpenCode сам выбирает провайдера по env (`openai` →
        Responses API), что у произвольных OpenAI-совместимых upstream'ов
        ломается на resume («Invalid Responses API request»). Пишем явный
        провайдер на @ai-sdk/openai-compatible (chat-completions) поверх
        bridge-endpoint'а из env; модель — из executor.external.model.
        """
        config: dict[str, object] = {
            "$schema": "https://opencode.ai/config.json",
            "plugin": ["/opt/superpowers/node_modules/superpowers"],
        }
        if model is not None:
            config["provider"] = {
                "svarog": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Svarog bridge",
                    "options": {
                        "baseURL": "{env:OPENAI_BASE_URL}",
                        "apiKey": "{env:OPENAI_API_KEY}",
                    },
                    "models": {model: {"name": model}},
                }
            }
            config["model"] = f"svarog/{model}"
        return {
            ".config/opencode/opencode.jsonc": json.dumps(config, ensure_ascii=False, indent=2)
            + "\n"
        }

    def mcp_client_config(self, url: str, token: str) -> dict[str, dict[str, Any]]:
        """Мост Svarog как remote-MCP в managed-конфиге OpenCode."""
        return {
            ".config/opencode/opencode.jsonc": {
                "mcp": {
                    "svarog": {
                        "type": "remote",
                        "url": url,
                        "headers": {"Authorization": f"Bearer {token}"},
                        "enabled": True,
                    }
                }
            }
        }

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
                # gpt-oss/harmony: часть провайдеров кладёт финальный ответ в
                # reasoning-канал при пустом content — executor держит последний
                # reasoning как фолбэк финала; в trace он не пишется.
                text = part.get("text")
                if isinstance(text, str) and text:
                    return [AgentEvent(kind="reasoning", text=text, session_id=session)]
                return []
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
