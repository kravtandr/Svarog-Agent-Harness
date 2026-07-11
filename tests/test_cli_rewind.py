"""Тесты `svarog rewind` (ADR-0015 фаза 5): turn-level git rewind по Run-Id trailer."""

import json
import subprocess
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
from svarog_harness.runtime import orchestrator

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


def _git(ws: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ws), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


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
    subprocess.run(["git", "-C", str(ws), "init", "-q"], check=True)
    _git(ws, "config", "user.name", "Test")
    _git(ws, "config", "user.email", "test@localhost")
    (ws / "README.md").write_text("# проект\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "init")
    monkeypatch.chdir(ws)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "200")
    return ws


def _patch_provider(monkeypatch: pytest.MonkeyPatch, turns: list[CompletionResult]) -> None:
    provider = ScriptedProvider(turns)

    def fake_default_provider(models_cfg: ModelsConfig, store: object = None) -> ModelProvider:
        return provider

    monkeypatch.setattr(orchestrator, "default_provider", fake_default_provider)


def _write_file_turn() -> CompletionResult:
    return CompletionResult(
        content="",
        tool_calls=(
            ToolCallRequest(
                id="c1",
                name="write_file",
                arguments_json='{"path": "result.txt", "content": "42"}',
            ),
        ),
        usage=Usage(10, 5),
        finish_reason="tool_calls",
    )


def _final(text: str) -> CompletionResult:
    return CompletionResult(content=text, usage=Usage(10, 5), finish_reason="stop")


def _run_and_get_id(monkeypatch: pytest.MonkeyPatch, with_write: bool = True) -> str:
    turns = [_write_file_turn(), _final("готово")] if with_write else [_final("готово")]
    _patch_provider(monkeypatch, turns)
    result = runner.invoke(cli_main.app, ["run", "задача", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["run_id"]


def test_rewind_reverts_run_commit(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _run_and_get_id(monkeypatch)
    assert (workspace / "result.txt").exists()
    head_before = _git(workspace, "rev-parse", "HEAD")

    result = runner.invoke(cli_main.app, ["rewind", run_id[:8], "--yes"])
    assert result.exit_code == 0, result.output
    assert not (workspace / "result.txt").exists()
    assert _git(workspace, "rev-parse", "HEAD") != head_before
    # README из начального коммита не пострадал.
    assert (workspace / "README.md").exists()


def test_rewind_refuses_dirty_tree(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _run_and_get_id(monkeypatch)
    (workspace / "незакоммиченный.txt").write_text("x", encoding="utf-8")

    result = runner.invoke(cli_main.app, ["rewind", run_id[:8], "--yes"])
    assert result.exit_code == 1
    assert (workspace / "result.txt").exists()  # ничего не откачено


def test_rewind_without_commits_fails(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _run_and_get_id(monkeypatch, with_write=False)
    result = runner.invoke(cli_main.app, ["rewind", run_id[:8], "--yes"])
    assert result.exit_code == 1
    assert "step-коммит" in result.output


def test_rewind_refuses_foreign_commits_on_top(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _run_and_get_id(monkeypatch)
    (workspace / "чужое.txt").write_text("вручную", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "ручной коммит поверх")

    result = runner.invoke(cli_main.app, ["rewind", run_id[:8], "--yes"])
    assert result.exit_code == 1
    assert (workspace / "result.txt").exists()
    assert (workspace / "чужое.txt").exists()


def test_rewind_unknown_run_fails(workspace: Path) -> None:
    result = runner.invoke(cli_main.app, ["rewind", "deadbeef", "--yes"])
    assert result.exit_code == 1
