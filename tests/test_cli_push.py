"""Тесты `svarog push`: отказ по protected-ветке даёт ненулевой exit code."""

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from svarog_harness.cli import main as cli_main

runner = CliRunner()


@pytest.fixture
def git_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "-C", str(ws), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.email", "t@localhost"], check=True)
    (ws / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-q", "-m", "init"], check=True)
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(ws)
    monkeypatch.setenv("HOME", str(tmp_path))
    return ws


def test_push_protected_branch_rejected_with_exit_code(git_workspace: Path) -> None:
    """Push в protected-ветку — critical-набор (§3.6): отказ и exit 1."""
    result = runner.invoke(cli_main.app, ["push", "main"])
    assert result.exit_code == 1
    assert "требует approval" in result.output


def test_push_outside_git_repo_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "plain"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(ws)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(cli_main.app, ["push", "main"])
    assert result.exit_code == 1
    assert "git-репозиторием" in result.output
