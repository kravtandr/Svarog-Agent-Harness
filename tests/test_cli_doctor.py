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


def test_find_agent_orphans_filters_dead_and_unlabeled() -> None:
    """Сироты = svarog-agent=1 без živого svarog-owner-pid (legacy-ресурсы до
    reaper'а не подметаются никогда — кампания 21.07.2026)."""
    import os
    import subprocess

    from svarog_harness.cli.doctor import find_agent_orphans

    outputs = {
        "ps": "svarog-old\t\nsvarog-dead\t99999999\nsvarog-mine\t" + str(os.getpid()) + "\n",
        "network": "svarog-net-old\t\n",
    }

    def fake_run(argv, **kwargs):
        key = "network" if "network" in argv else "ps"
        return subprocess.CompletedProcess(argv, 0, stdout=outputs[key], stderr="")

    containers, networks = find_agent_orphans(run=fake_run)
    assert containers == ["svarog-old", "svarog-dead"]  # без метки и мёртвый pid
    assert "svarog-mine" not in containers  # живой владелец — не сирота
    assert networks == ["svarog-net-old"]


def test_remove_agent_orphans_invokes_docker_rm() -> None:
    from svarog_harness.cli.doctor import remove_agent_orphans

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        import subprocess

        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    remove_agent_orphans(["c1", "c2"], ["n1"], run=fake_run)
    assert ["docker", "rm", "-f", "c1", "c2"] in calls
    assert ["docker", "network", "rm", "n1"] in calls
