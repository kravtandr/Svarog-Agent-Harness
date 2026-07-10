from typer.testing import CliRunner

from svarog_harness import __version__
from svarog_harness.cli.main import app

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "svarog" in result.output.lower()


def test_serve_refuses_external_bind_without_gateway_token(tmp_path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
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
    monkeypatch.setenv("HOME", str(tmp_path))

    result = runner.invoke(app, ["serve", "--host", "0.0.0.0", "--workspace", str(ws)])

    assert result.exit_code == 1
    assert "gateway.token_ref" in result.output


def _write_config_with_memory(ws, tmp_path) -> None:
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        "memory:\n  path: ./memory\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )


def test_memory_curate_reports_orphan(tmp_path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config_with_memory(ws, tmp_path)
    (ws / "memory" / "projects" / "ghost").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(ws)

    result = runner.invoke(app, ["memory", "curate"])

    assert result.exit_code == 0, result.output
    assert "orphan" in result.output
    assert (ws / "artifacts").is_dir()  # отчёт записан


def test_memory_curate_clean(tmp_path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config_with_memory(ws, tmp_path)
    (ws / "memory" / "user").mkdir(parents=True)
    (ws / "memory" / "user" / "profile.md").write_text("факт\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(ws)

    result = runner.invoke(app, ["memory", "curate"])

    assert result.exit_code == 0, result.output
    assert "находок нет" in result.output
