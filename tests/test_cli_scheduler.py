"""CLI планировщика: группа `cron` (блок D §9)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from svarog_harness.cli.main import app

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


def _add_job(workspace: Path, *, name: str = "ночная", every: str = "3600") -> str:
    result = runner.invoke(
        app,
        ["cron", "add", name, "--task", "собери сводку", "--every", every],
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_cron_add_creates_disabled_job(workspace: Path) -> None:
    """Джоба заводится выключенной: активация — отдельный явный шаг."""
    output = _add_job(workspace)
    assert "выключена" in output

    listed = runner.invoke(app, ["cron", "list"])
    assert listed.exit_code == 0, listed.output
    assert "ночная" in listed.output
    assert "нет" in listed.output  # колонка «включена»


def test_cron_enable_then_disable(workspace: Path) -> None:
    _add_job(workspace)
    job_id = _job_id(workspace)

    enabled = runner.invoke(app, ["cron", "enable", job_id])
    assert enabled.exit_code == 0, enabled.output
    assert "включена" in enabled.output

    disabled = runner.invoke(app, ["cron", "disable", job_id])
    assert disabled.exit_code == 0, disabled.output
    assert "выключена" in disabled.output


def test_cron_remove_deletes_job(workspace: Path) -> None:
    _add_job(workspace)
    job_id = _job_id(workspace)

    removed = runner.invoke(app, ["cron", "remove", job_id])
    assert removed.exit_code == 0, removed.output

    listed = runner.invoke(app, ["cron", "list"])
    assert "ночная" not in listed.output


def test_cron_add_rejects_bad_schedule(workspace: Path) -> None:
    result = runner.invoke(app, ["cron", "add", "плохая", "--task", "t", "--at", "25:00"])
    assert result.exit_code != 0
    assert "HH:MM" in result.output


def test_cron_add_requires_one_schedule(workspace: Path) -> None:
    """Ни одного расписания — ошибка: молча выбранное умолчание тут опасно."""
    result = runner.invoke(app, ["cron", "add", "без расписания", "--task", "t"])
    assert result.exit_code != 0
    assert "--every" in result.output


def _job_id(workspace: Path) -> str:
    listed = runner.invoke(app, ["cron", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    import json

    first = json.loads(listed.output.strip().splitlines()[0])
    return str(first["id"])
