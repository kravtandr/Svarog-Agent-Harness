"""Адаптер Codex CLI (ADR-0016 фаза 4): `codex exec --json` → AgentEvent.

Формат стрима (JSONL, по объекту на строку):

* `{"type":"thread.started","thread_id":…}` — старт треда (session id);
* `{"type":"item.started"|"item.updated"|"item.completed","item":{…}}` —
  элементы хода: `agent_message` (текст), `command_execution` (команда,
  aggregated_output, exit_code), `file_change`, `mcp_tool_call`,
  `reasoning`, `todo_list`;
* `{"type":"turn.completed","usage":{"input_tokens":…,"output_tokens":…}}`;
* `{"type":"turn.failed","error":{"message":…}}` / `{"type":"error",…}`.

Финального текста в turn.completed нет — executor берёт последнюю
text-реплику (fallback в _StreamState.last_text).
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

_STATE_DIR = PurePosixPath("/tmp/home/.codex")


class CodexAdapter:
    def __init__(self, binary: str = "codex") -> None:
        self._binary = binary

    @property
    def name(self) -> str:
        return "codex"

    @property
    def wire_format(self) -> str:
        return "openai"

    def capabilities(self) -> AdapterCapabilities:
        # hooks/mcp: у Codex нет permission-хуков, а его MCP-конфиг (TOML,
        # stdio) не совместим с HTTP-bridge Svarog — честно False: supervised
        # отклоняется fail-closed, память/скиллы не пробрасываются (§6).
        return AdapterCapabilities(hooks=False, resume=True, mcp=False)

    def command(self, launch: AgentLaunch) -> list[str]:
        argv = [self._binary, "exec"]
        if launch.session is not None:
            argv += ["resume", launch.session]
        argv += [
            "--json",
            "--skip-git-repo-check",
            # Tier 1 (ADR-0016 §6): границу держит sandbox Svarog, встроенная
            # песочница Codex внутри контейнера без сети не работает.
            "--dangerously-bypass-approvals-and-sandbox",
            launch.task,
        ]
        return argv

    def base_url_env(self, auth: AgentAuth) -> dict[str, str]:
        # Codex ходит по OpenAI wire-протоколу; ключ — per-run токен bridge,
        # настоящий инжектирует прокси (§3). subscription не поддержан
        # (валидатор конфига это уже отклонил) — только api-key.
        return {
            "OPENAI_BASE_URL": auth.base_url + "/v1",
            "OPENAI_API_KEY": auth.proxy_token,
        }

    def state_dir(self) -> PurePosixPath:
        return _STATE_DIR

    def context_files(self, memory: str, skill_cards: str) -> dict[str, str]:
        """~/.codex/AGENTS.md — глобальная инструкция Codex."""
        sections: list[str] = []
        if memory:
            sections.append(f"# Память Svarog\n\n{memory}")
        if skill_cards:
            sections.append(f"# Скиллы Svarog\n\n{skill_cards}")
        if not sections:
            return {}
        return {"AGENTS.md": "\n\n".join(sections) + "\n"}

    def provider_files(self, model: str | None) -> dict[str, str]:
        # Провайдер/модель Codex задаются его собственным конфигом в state.
        return {}

    def mcp_client_config(self, url: str, token: str) -> dict[str, dict[str, Any]]:
        # MCP не поддерживается (capabilities.mcp=False).
        return {}

    def managed_policy(self, mcp_config: str | None, hook_command: str | None) -> str | None:
        return None  # у Codex нет managed-настроек с высшим приоритетом

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
        match payload.get("type"):
            case "thread.started":
                return [
                    AgentEvent(
                        kind="opaque",
                        session_id=_str_or_none(payload.get("thread_id")),
                        raw=payload,
                    )
                ]
            case "item.started" | "item.updated" | "item.completed":
                return self._parse_item(payload)
            case "turn.completed":
                usage = payload.get("usage")
                usage = usage if isinstance(usage, dict) else {}
                return [
                    AgentEvent(
                        kind="result",
                        ok=True,
                        input_tokens=_int(usage.get("input_tokens")),
                        output_tokens=_int(usage.get("output_tokens")),
                        raw=payload,
                    )
                ]
            case "turn.failed":
                error = payload.get("error")
                message = error.get("message") if isinstance(error, dict) else ""
                return [AgentEvent(kind="result", ok=False, text=str(message or ""), raw=payload)]
            case "turn.started":
                return []
            case _:
                return [AgentEvent(kind="opaque", raw=payload)]

    def _parse_item(self, payload: dict[str, Any]) -> list[AgentEvent]:
        item = payload.get("item")
        if not isinstance(item, dict):
            return [AgentEvent(kind="opaque", raw=payload)]
        completed = payload.get("type") == "item.completed"
        item_id = _str_or_none(item.get("id"))
        match item.get("type"):
            case "agent_message":
                text = item.get("text")
                if completed and isinstance(text, str) and text:
                    return [AgentEvent(kind="text", text=text)]
                return []
            case "command_execution":
                if payload.get("type") == "item.started":
                    return [
                        AgentEvent(
                            kind="tool_call",
                            tool_name="command_execution",
                            call_id=item_id,
                            arguments={"command": str(item.get("command", ""))},
                        )
                    ]
                if completed:
                    return [
                        AgentEvent(
                            kind="tool_result",
                            call_id=item_id,
                            text=str(item.get("aggregated_output", "")),
                            ok=item.get("status") == "completed"
                            and _int(item.get("exit_code")) == 0,
                        )
                    ]
                return []
            case "mcp_tool_call":
                if payload.get("type") == "item.started":
                    arguments = item.get("arguments")
                    return [
                        AgentEvent(
                            kind="tool_call",
                            tool_name=f"mcp:{item.get('server', '')}.{item.get('tool', '')}",
                            call_id=item_id,
                            arguments=arguments if isinstance(arguments, dict) else {},
                        )
                    ]
                if completed:
                    return [
                        AgentEvent(
                            kind="tool_result",
                            call_id=item_id,
                            text=_mcp_result_text(item.get("result")),
                            ok=item.get("status") == "completed",
                        )
                    ]
                return []
            case "file_change":
                if completed:
                    changes = item.get("changes")
                    return [
                        AgentEvent(
                            kind="tool_call",
                            tool_name="file_change",
                            arguments={"changes": changes if isinstance(changes, list) else []},
                        )
                    ]
                return []
            case "reasoning":
                # Фолбэк финала (см. AgentEventKind): reasoning не пишется в
                # trace, но executor держит последний на случай пустого ответа.
                text = item.get("text")
                if completed and isinstance(text, str) and text:
                    return [AgentEvent(kind="reasoning", text=text)]
                return []
            case "todo_list":
                return []  # не событие trace
            case _:
                return [AgentEvent(kind="opaque", raw=payload)]


def _mcp_result_text(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part)


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
