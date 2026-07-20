"""Golden-тесты адаптеров codex/opencode (ADR-0016 фаза 4): контракт
парсеров закреплён дословными JSONL-фикстурами стримов."""

import json

from svarog_harness.config.schema import ExternalExecutorConfig
from svarog_harness.runtime.agents import (
    CLIENT_GATE_TIMEOUT_MARGIN_SEC,
    CodexAdapter,
    OpencodeAdapter,
    adapter_for,
)
from svarog_harness.runtime.executor import AgentAuth, AgentLaunch

# --- Codex -------------------------------------------------------------------

_CODEX_STREAM = [
    {"type": "thread.started", "thread_id": "0199a213-81c0-7800-8aa1-bbab2a035a53"},
    {"type": "turn.started"},
    {
        "type": "item.started",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "bash -lc ls",
            "aggregated_output": "",
            "exit_code": None,
            "status": "in_progress",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "bash -lc ls",
            "aggregated_output": "docs\nsrc\n",
            "exit_code": 0,
            "status": "completed",
        },
    },
    {
        "type": "item.completed",
        "item": {"id": "item_2", "type": "reasoning", "text": "**Thinking**"},
    },
    {
        "type": "item.completed",
        "item": {"id": "item_3", "type": "agent_message", "text": "Готово: docs и src на месте."},
    },
    {
        "type": "turn.completed",
        "usage": {"input_tokens": 24763, "cached_input_tokens": 24448, "output_tokens": 122},
    },
]


def test_codex_stream_golden() -> None:
    adapter = CodexAdapter()
    events = [e for line in _CODEX_STREAM for e in adapter.parse_event(json.dumps(line))]
    kinds = [e.kind for e in events]
    assert kinds == ["opaque", "tool_call", "tool_result", "reasoning", "text", "result"]
    assert events[0].session_id == "0199a213-81c0-7800-8aa1-bbab2a035a53"
    call, result_ev = events[1], events[2]
    assert call.tool_name == "command_execution"
    assert call.call_id == "item_1"
    assert call.arguments == {"command": "bash -lc ls"}
    assert result_ev.call_id == "item_1"
    assert result_ev.ok
    assert "docs" in result_ev.text
    assert events[3].text == "**Thinking**"
    assert events[4].text == "Готово: docs и src на месте."
    final = events[5]
    assert final.ok
    assert (final.input_tokens, final.output_tokens) == (24763, 122)


def test_codex_turn_failed() -> None:
    (event,) = CodexAdapter().parse_event(
        json.dumps({"type": "turn.failed", "error": {"message": "model response stream ended"}})
    )
    assert event.kind == "result"
    assert not event.ok
    assert "stream ended" in event.text


def test_codex_command_and_resume() -> None:
    adapter = CodexAdapter()
    argv = adapter.command(AgentLaunch(task="сделай файл"))
    assert argv[:2] == ["codex", "exec"]
    assert "--json" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv  # tier 1: границу держит sandbox
    assert argv[-1] == "сделай файл"
    resumed = adapter.command(AgentLaunch(task="продолжай", session="thread-1"))
    assert resumed[:4] == ["codex", "exec", "resume", "thread-1"]


# --- OpenCode ------------------------------------------------------------------

_OPENCODE_STREAM = [
    {
        "type": "step_start",
        "timestamp": 1770000000000,
        "sessionID": "ses_494719016ffe85dkDMj0FPRbHK",
        "part": {"id": "prt_1", "type": "step-start"},
    },
    {
        "type": "reasoning",
        "timestamp": 1770000000100,
        "sessionID": "ses_494719016ffe85dkDMj0FPRbHK",
        "part": {"text": "думаю"},
    },
    {
        "type": "tool_use",
        "timestamp": 1770000000200,
        "sessionID": "ses_494719016ffe85dkDMj0FPRbHK",
        "part": {
            "id": "prt_2",
            "tool": "bash",
            "state": {
                "status": "completed",
                "input": {"command": "ls"},
                "output": "hello.py",
            },
        },
    },
    {
        "type": "text",
        "timestamp": 1770000000300,
        "sessionID": "ses_494719016ffe85dkDMj0FPRbHK",
        "part": {"id": "prt_3", "text": "Файл на месте."},
    },
    {
        "type": "step_finish",
        "timestamp": 1770000000400,
        "sessionID": "ses_494719016ffe85dkDMj0FPRbHK",
        "reason": "stop",
        "cost": 0.0123,
        "tokens": {"input": 900, "output": 80, "reasoning": 10},
    },
]


def test_opencode_stream_golden() -> None:
    adapter = OpencodeAdapter()
    events = [e for line in _OPENCODE_STREAM for e in adapter.parse_event(json.dumps(line))]
    kinds = [e.kind for e in events]
    assert kinds == ["opaque", "reasoning", "tool_call", "tool_result", "text", "result"]
    assert events[0].session_id == "ses_494719016ffe85dkDMj0FPRbHK"
    assert events[1].text == "думаю"
    call, result_ev = events[2], events[3]
    assert call.tool_name == "bash"
    assert call.call_id == "prt_2"
    assert call.arguments == {"command": "ls"}
    assert result_ev.ok and result_ev.text == "hello.py"
    assert events[4].text == "Файл на месте."
    final = events[5]
    assert final.ok
    assert (final.input_tokens, final.output_tokens) == (900, 80)
    assert final.cost_usd == 0.0123


def test_opencode_intermediate_step_finish_skipped() -> None:
    payload = {
        "type": "step_finish",
        "sessionID": "ses_1",
        "reason": "tool-calls",
        "tokens": {"input": 10, "output": 2},
    }
    assert OpencodeAdapter().parse_event(json.dumps(payload)) == []


def test_opencode_error_event() -> None:
    payload = {
        "type": "error",
        "sessionID": "ses_1",
        "error": {"name": "ProviderError", "data": {"message": "нет доступа к модели"}},
    }
    (event,) = OpencodeAdapter().parse_event(json.dumps(payload))
    assert event.kind == "result"
    assert not event.ok
    assert "нет доступа" in event.text


def test_opencode_command_and_resume() -> None:
    adapter = OpencodeAdapter()
    argv = adapter.command(AgentLaunch(task="почини тест"))
    assert argv[:3] == ["opencode", "run", "почини тест"]
    assert "--format" in argv and "json" in argv
    resumed = adapter.command(AgentLaunch(task="дальше", session="ses_2"))
    assert resumed[-2:] == ["--session", "ses_2"]


# --- Матрица capabilities (§1/§6) ---------------------------------------------


def test_claude_subscription_env() -> None:
    from svarog_harness.runtime.agents.claude_code import ClaudeCodeAdapter

    adapter = ClaudeCodeAdapter()
    api = adapter.base_url_env(
        AgentAuth(base_url="http://bridge:8080", proxy_token="run-tok", mode="api-key")
    )
    assert api["ANTHROPIC_API_KEY"] == "run-tok"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in api

    sub = adapter.base_url_env(
        AgentAuth(
            base_url="http://bridge:8080",
            proxy_token="run-tok",
            mode="subscription",
            credential="sk-ant-oat01-real",
        )
    )
    assert sub["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-real"
    # ANTHROPIC_API_KEY НЕ ставится — иначе перебил бы OAuth (precedence).
    assert "ANTHROPIC_API_KEY" not in sub
    assert sub["ANTHROPIC_BASE_URL"] == "http://bridge:8080"
    # Containment: нативная auto-память отключена в обоих режимах, чтобы факты
    # шли через mcp__svarog__remember, а не в ~/.claude/…/memory (регрессия S4).
    assert api["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert sub["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_claude_context_steers_memory_to_mcp() -> None:
    """Регрессия S4: инжектируемый контекст велит писать память через MCP-tool,
    а не в файлы; присутствует всегда, память подставляется, когда есть."""
    from svarog_harness.runtime.agents.claude_code import ClaudeCodeAdapter

    adapter = ClaudeCodeAdapter()
    empty = adapter.context_files(memory="", skill_cards="")
    assert "mcp__svarog__remember" in empty["CLAUDE.md"]

    full = adapter.context_files(memory="- forge-01 — рабочий ноутбук", skill_cards="")
    body = full["CLAUDE.md"]
    assert "mcp__svarog__remember" in body
    assert "forge-01" in body


def test_capability_matrix() -> None:
    claude = adapter_for(ExternalExecutorConfig(image="i", adapter="claude-code"))
    codex = adapter_for(ExternalExecutorConfig(image="i", adapter="codex"))
    opencode = adapter_for(ExternalExecutorConfig(image="i", adapter="opencode"))
    # Полный tier 2 — только у claude-code; supervised с другими — fail-closed.
    assert claude.capabilities().hooks and claude.capabilities().mcp
    assert not codex.capabilities().hooks and not codex.capabilities().mcp
    assert not opencode.capabilities().hooks and not opencode.capabilities().mcp
    assert all(a.capabilities().resume for a in (claude, codex, opencode))
    assert claude.wire_format == "anthropic"
    assert codex.wire_format == "openai" and opencode.wire_format == "openai"


def test_claude_hook_timeout_exceeds_grace() -> None:
    # Клиентский лимит PreToolUse-хука обязан переживать grace-ожидание
    # approval (§7), иначе хук умирает раньше suspend и гейт не срабатывает.
    cfg = ExternalExecutorConfig(image="i", adapter="claude-code", approval_grace_sec=900)
    adapter = adapter_for(cfg)
    managed = json.loads(adapter.managed_policy(None, "python3 /run/svarog/hook.py") or "{}")
    hook = managed["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["timeout"] == 900 + CLIENT_GATE_TIMEOUT_MARGIN_SEC
    assert hook["timeout"] > cfg.approval_grace_sec


def test_opencode_provider_files_pin_chat_completions_provider() -> None:
    # Регрессия: без managed-конфига OpenCode выбирает провайдера `openai`
    # (Responses API), и resume у OpenAI-совместимых upstream'ов падает
    # («Invalid Responses API request»). С executor.external.model Svarog
    # пишет провайдера на @ai-sdk/openai-compatible (chat-completions).
    files = OpencodeAdapter().provider_files("openai/gpt-oss-120b")
    assert list(files) == [".config/opencode/opencode.jsonc"]
    config = json.loads(files[".config/opencode/opencode.jsonc"])
    provider = config["provider"]["svarog"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "{env:OPENAI_BASE_URL}"
    assert "openai/gpt-oss-120b" in provider["models"]
    assert config["model"] == "svarog/openai/gpt-oss-120b"


def test_opencode_provider_files_absent_without_model() -> None:
    assert OpencodeAdapter().provider_files(None) == {}
