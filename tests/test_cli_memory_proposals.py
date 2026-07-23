"""CLI-ревью memory proposals (блок C §5)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from svarog_harness.cli.main import app

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    memory = tmp_path / "memory"
    memory.mkdir()
    db_path = tmp_path / "state" / "svarog.db"
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"memory:\n  path: {memory}\n"
        f"storage:\n  db_path: {db_path}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(ws)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "200")
    return ws


def test_proposals_list_on_empty_db(workspace: Path) -> None:
    result = runner.invoke(app, ["memory", "proposals", "list"])
    assert result.exit_code == 0, result.output
    assert "ожидающих" in result.output


def test_show_unknown_id_exits_with_error(workspace: Path) -> None:
    result = runner.invoke(app, ["memory", "proposals", "show", "deadbeef"])
    assert result.exit_code == 1
    assert "не найден" in result.output


def test_approve_unknown_id_exits_with_error(workspace: Path) -> None:
    result = runner.invoke(app, ["memory", "proposals", "approve", "deadbeef"])
    assert result.exit_code == 1
    assert "не найден" in result.output


def test_commands_refuse_when_memory_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без настроенной памяти ревью нечего показывать — честный отказ, а не пустой список."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(ws)
    monkeypatch.setenv("HOME", str(tmp_path))

    result = runner.invoke(app, ["memory", "proposals", "list"])
    assert result.exit_code == 1
    assert "память не настроена" in result.output
