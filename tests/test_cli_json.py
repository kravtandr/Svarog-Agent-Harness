"""Тесты JSON/NDJSON-вывода CLI (ADR-0015 фаза 5): run/resume/traces --json."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from svarog_harness.cli import main as cli_main
from svarog_harness.config.schema import ModelsConfig
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.runtime import run_assembly

runner = CliRunner()


class ScriptedProvider(ModelProvider):
    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        return self.turns.pop(0)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    db_path = tmp_path / "state" / "svarog.db"
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {db_path}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(ws)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "200")
    return ws


def _patch_provider(monkeypatch: pytest.MonkeyPatch, turns: list[CompletionResult]) -> None:
    provider = ScriptedProvider(turns)

    def fake_default_provider(models_cfg: ModelsConfig, store: object = None) -> ModelProvider:
        return provider

    monkeypatch.setattr(run_assembly, "default_provider", fake_default_provider)


def _final(text: str) -> CompletionResult:
    return CompletionResult(content=text, usage=Usage(10, 5), finish_reason="stop")


def _tool_turn(*calls: ToolCallRequest) -> CompletionResult:
    return CompletionResult(
        content="", tool_calls=calls, usage=Usage(10, 5), finish_reason="tool_calls"
    )


def test_run_json_completed(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_provider(monkeypatch, [_final("Готово!")])
    result = runner.invoke(cli_main.app, ["run", "задача", "--json"])
    assert result.exit_code == 0, result.output
    # Весь stdout — один JSON-объект, без rich-шума вокруг.
    payload = json.loads(result.output)
    assert payload["state"] == "completed"
    assert payload["final_answer"] == "Готово!"
    assert payload["iterations"] == 1
    assert payload["run_id"]
    assert payload["cost_usd"] == pytest.approx(0.0)


def test_run_json_suspended_keeps_exit_code(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(
        monkeypatch,
        [
            _tool_turn(ToolCallRequest(id="c1", name="list_dir", arguments_json="{}")),
            _tool_turn(ToolCallRequest(id="c2", name="list_dir", arguments_json="{}")),
        ],
    )
    monkeypatch.setenv("SVAROG_RUNTIME__MAX_ITERATIONS", "2")
    monkeypatch.setenv("SVAROG_RUNTIME__REFUEL_AFTER_ITERATIONS", "5")
    monkeypatch.setenv("SVAROG_RUNTIME__STAGNATION_REPEATS", "100")
    result = runner.invoke(cli_main.app, ["run", "задача", "--json"])
    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert payload["state"] == "suspended"


def test_resume_json(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_provider(
        monkeypatch,
        [
            _tool_turn(ToolCallRequest(id="c1", name="list_dir", arguments_json="{}")),
            _tool_turn(ToolCallRequest(id="c2", name="list_dir", arguments_json="{}")),
            _final("после resume"),
        ],
    )
    monkeypatch.setenv("SVAROG_RUNTIME__REFUEL_AFTER_ITERATIONS", "5")
    monkeypatch.setenv("SVAROG_RUNTIME__STAGNATION_REPEATS", "100")
    monkeypatch.setenv("SVAROG_RUNTIME__MAX_ITERATIONS", "2")
    first = runner.invoke(cli_main.app, ["run", "задача", "--json"])
    assert first.exit_code == 3
    run_id = json.loads(first.output)["run_id"]

    monkeypatch.setenv("SVAROG_RUNTIME__MAX_ITERATIONS", "10")
    result = runner.invoke(cli_main.app, ["resume", run_id[:8], "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["state"] == "completed"
    assert payload["final_answer"] == "после resume"


def test_traces_list_ndjson(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_provider(monkeypatch, [_final("раз"), _final("два")])
    assert runner.invoke(cli_main.app, ["run", "первая", "--json"]).exit_code == 0
    assert runner.invoke(cli_main.app, ["run", "вторая", "--json"]).exit_code == 0

    result = runner.invoke(cli_main.app, ["traces", "list", "--json"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    rows = [json.loads(line) for line in lines]  # NDJSON: по объекту на строку
    assert len(rows) == 2
    assert {row["task"] for row in rows} == {"первая", "вторая"}
    assert all(row["state"] == "completed" for row in rows)
    assert all("created_at" in row for row in rows)


def test_traces_show_json(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_provider(
        monkeypatch,
        [
            _tool_turn(ToolCallRequest(id="c1", name="list_dir", arguments_json="{}")),
            _final("готово"),
        ],
    )
    first = runner.invoke(cli_main.app, ["run", "задача", "--json"])
    run_id = json.loads(first.output)["run_id"]

    result = runner.invoke(cli_main.app, ["traces", "show", run_id[:8], "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run"]["id"] == run_id
    assert payload["run"]["state"] == "completed"
    assert any(tc["tool_name"] == "list_dir" for tc in payload["tool_calls"])
    roles = [m["role"] for m in payload["messages"]]
    assert "assistant" in roles and "tool" in roles
