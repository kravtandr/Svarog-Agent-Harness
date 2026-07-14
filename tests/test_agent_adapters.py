"""Golden-тесты адаптеров codex/opencode (ADR-0016 фаза 4): контракт
парсеров закреплён дословными JSONL-фикстурами стримов."""

import json

from svarog_harness.config.schema import ExternalExecutorConfig
from svarog_harness.runtime.agents import CodexAdapter, OpencodeAdapter, adapter_for
from svarog_harness.runtime.executor import AgentLaunch

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
    assert kinds == ["opaque", "tool_call", "tool_result", "text", "result"]
    assert events[0].session_id == "0199a213-81c0-7800-8aa1-bbab2a035a53"
    call, result_ev = events[1], events[2]
    assert call.tool_name == "command_execution"
    assert call.call_id == "item_1"
    assert call.arguments == {"command": "bash -lc ls"}
    assert result_ev.call_id == "item_1"
    assert result_ev.ok
    assert "docs" in result_ev.text
    assert events[3].text == "Готово: docs и src на месте."
    final = events[4]
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
    assert kinds == ["opaque", "tool_call", "tool_result", "text", "result"]
    assert events[0].session_id == "ses_494719016ffe85dkDMj0FPRbHK"
    call, result_ev = events[1], events[2]
    assert call.tool_name == "bash"
    assert call.call_id == "prt_2"
    assert call.arguments == {"command": "ls"}
    assert result_ev.ok and result_ev.text == "hello.py"
    assert events[3].text == "Файл на месте."
    final = events[4]
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
