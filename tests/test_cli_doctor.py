"""Тесты `svarog doctor` (ADR-0015 фаза 5): диагностика окружения, read-only."""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from svarog_harness.cli import main as cli_main

runner = CliRunner()


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


def test_doctor_healthy_workspace_exits_zero(workspace: Path) -> None:
    result = runner.invoke(cli_main.app, ["doctor"])
    assert result.exit_code == 0, result.output
    for check in ("config", "git", "db", "sandbox", "model", "ripgrep"):
        assert check in result.output


def test_doctor_json_output(workspace: Path) -> None:
    result = runner.invoke(cli_main.app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    checks = json.loads(result.output)
    names = {c["name"] for c in checks}
    assert {"config", "git", "db", "sandbox", "model", "ripgrep"} <= names
    assert all(c["status"] in {"ok", "warn", "fail"} for c in checks)


def test_doctor_broken_config_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text("models: [не мапа\n", encoding="utf-8")
    monkeypatch.chdir(ws)
    monkeypatch.setenv("HOME", str(tmp_path))

    result = runner.invoke(cli_main.app, ["doctor"])
    assert result.exit_code == 1
    assert "config" in result.output
    # Битый конфиг не мешает env-проверкам (git/ripgrep всё равно в отчёте).
    assert "git" in result.output


def test_doctor_missing_api_key_fails(workspace: Path) -> None:
    (workspace / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "      api_key_ref: NO_SUCH_KEY\n"
        "sandbox:\n  type: local-trusted\n",
        encoding="utf-8",
    )
    result = runner.invoke(cli_main.app, ["doctor"])
    assert result.exit_code == 1
    assert "NO_SUCH_KEY" in result.output


def test_doctor_docker_missing_fails(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (workspace / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: docker\n",
        encoding="utf-8",
    )
    from svarog_harness.cli import doctor as doctor_mod

    real_which = shutil.which
    monkeypatch.setattr(
        doctor_mod.shutil, "which", lambda name: None if name == "docker" else real_which(name)
    )
    result = runner.invoke(cli_main.app, ["doctor"])
    assert result.exit_code == 1
    assert "docker" in result.output


def test_doctor_missing_rg_warns_but_passes(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from svarog_harness.cli import doctor as doctor_mod

    real_which = shutil.which
    monkeypatch.setattr(
        doctor_mod.shutil, "which", lambda name: None if name == "rg" else real_which(name)
    )
    result = runner.invoke(cli_main.app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    checks = {c["name"]: c for c in json.loads(result.output)}
    assert checks["ripgrep"]["status"] == "warn"


def test_doctor_is_read_only(workspace: Path, tmp_path: Path) -> None:
    """doctor не создаёт БД и ничего не пишет (в отличие от init/run)."""
    result = runner.invoke(cli_main.app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "state" / "svarog.db").exists()
