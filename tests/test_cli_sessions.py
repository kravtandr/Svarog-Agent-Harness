"""Тесты sessions CLI (ADR-0015 фаза 5): list/search/rename, chat --session/--fork."""

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
    ToolDefinition,
    Usage,
)
from svarog_harness.runtime import orchestrator

runner = CliRunner()


class ScriptedProvider(ModelProvider):
    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)
        self.seen: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.seen.append(list(messages))
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


def _patch_provider(
    monkeypatch: pytest.MonkeyPatch, turns: list[CompletionResult]
) -> ScriptedProvider:
    provider = ScriptedProvider(turns)

    def fake_default_provider(models_cfg: ModelsConfig, store: object = None) -> ModelProvider:
        return provider

    monkeypatch.setattr(orchestrator, "default_provider", fake_default_provider)
    return provider


def _final(text: str) -> CompletionResult:
    return CompletionResult(content=text, usage=Usage(10, 5), finish_reason="stop")


def test_sessions_list_search_and_json(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_provider(monkeypatch, [_final("раз"), _final("два")])
    assert runner.invoke(cli_main.app, ["run", "первая задача", "--json"]).exit_code == 0
    assert runner.invoke(cli_main.app, ["run", "вторая задача", "--json"]).exit_code == 0

    result = runner.invoke(cli_main.app, ["sessions", "list"])
    assert result.exit_code == 0, result.output
    assert "первая задача" in result.output
    assert "вторая задача" in result.output

    result = runner.invoke(cli_main.app, ["sessions", "list", "--search", "первая"])
    assert result.exit_code == 0, result.output
    assert "первая задача" in result.output
    assert "вторая задача" not in result.output

    result = runner.invoke(cli_main.app, ["sessions", "list", "--json"])
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert len(rows) == 2
    assert all({"id", "title", "runs", "last_task", "updated_at"} <= row.keys() for row in rows)


def test_sessions_rename(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_provider(monkeypatch, [_final("ок")])
    assert runner.invoke(cli_main.app, ["run", "задача", "--json"]).exit_code == 0
    listed = runner.invoke(cli_main.app, ["sessions", "list", "--json"])
    session_id = json.loads(listed.output.splitlines()[0])["id"]

    result = runner.invoke(cli_main.app, ["sessions", "rename", session_id[:8], "Мой диалог"])
    assert result.exit_code == 0, result.output

    listed = runner.invoke(cli_main.app, ["sessions", "list", "--json"])
    assert json.loads(listed.output.splitlines()[0])["title"] == "Мой диалог"


def test_sessions_rename_unknown_fails(workspace: Path) -> None:
    result = runner.invoke(cli_main.app, ["sessions", "rename", "deadbeef", "имя"])
    assert result.exit_code == 1


def test_chat_continues_session_with_history(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _patch_provider(monkeypatch, [_final("Привет, Аня!"), _final("Тебя зовут Аня.")])
    assert runner.invoke(cli_main.app, ["run", "меня зовут Аня", "--json"]).exit_code == 0
    listed = runner.invoke(cli_main.app, ["sessions", "list", "--json"])
    session_id = json.loads(listed.output.splitlines()[0])["id"]

    result = runner.invoke(
        cli_main.app,
        ["chat", "--session", session_id[:8]],
        input="как меня зовут?\n\n",
    )
    assert result.exit_code == 0, result.output
    # Продолженная сессия видит прошлый диалог в контексте.
    chat_context = " ".join(m.content for m in provider.seen[-1])
    assert "меня зовут Аня" in chat_context
    assert "Привет, Аня!" in chat_context

    # Оба run'а — в одной session.
    traces = runner.invoke(cli_main.app, ["traces", "list", "--json"])
    session_ids = {json.loads(line)["session_id"] for line in traces.output.splitlines() if line}
    assert session_ids == {session_id}


def test_chat_fork_copies_history_into_new_session(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _patch_provider(monkeypatch, [_final("Привет, Аня!"), _final("Тебя зовут Аня.")])
    assert runner.invoke(cli_main.app, ["run", "меня зовут Аня", "--json"]).exit_code == 0
    listed = runner.invoke(cli_main.app, ["sessions", "list", "--json"])
    session_id = json.loads(listed.output.splitlines()[0])["id"]

    result = runner.invoke(
        cli_main.app,
        ["chat", "--fork", session_id[:8]],
        input="как меня зовут?\n\n",
    )
    assert result.exit_code == 0, result.output
    # История скопирована…
    chat_context = " ".join(m.content for m in provider.seen[-1])
    assert "меня зовут Аня" in chat_context
    # …но run форка живёт в НОВОЙ session (исходная не растёт).
    traces = runner.invoke(cli_main.app, ["traces", "list", "--json"])
    session_ids = {json.loads(line)["session_id"] for line in traces.output.splitlines() if line}
    assert len(session_ids) == 2


def test_chat_session_and_fork_are_mutually_exclusive(workspace: Path) -> None:
    result = runner.invoke(cli_main.app, ["chat", "--session", "a", "--fork", "b"])
    assert result.exit_code == 1
