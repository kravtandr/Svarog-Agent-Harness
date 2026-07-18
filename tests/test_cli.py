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


def test_chat_rejects_workspace_overlapping_control_plane(tmp_path, monkeypatch) -> None:
    # Регрессия: `chat` обходил assert_workspace_isolated (ADR-0015 §0.3),
    # который `run`/`resume` уже проверяют — memory внутри workspace под
    # docker-sandbox должна отклоняться так же, как в run_once.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: docker\n"
        "memory:\n  path: ./memory\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(ws)

    result = runner.invoke(app, ["chat", "--workspace", str(ws)])

    assert result.exit_code == 1, result.output
    assert "раскладки workspace" in result.output


def _write_chat_config(tmp_path, ws) -> None:
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


def test_chat_opens_tui_on_tty_and_plain_forces_repl(tmp_path, monkeypatch) -> None:
    # TTY-автовыбор (ADR-0018): с терминалом chat уходит в TUI, --plain и
    # отсутствие TTY остаются на построчном REPL.
    from svarog_harness.cli import main as cli_main
    from svarog_harness.cli import tui as tui_module

    ws = tmp_path / "ws"
    ws.mkdir()
    _write_chat_config(tmp_path, ws)
    monkeypatch.setenv("HOME", str(tmp_path))

    launched: list[tuple] = []
    monkeypatch.setattr(
        tui_module, "run_chat_tui", lambda *args, **kwargs: launched.append((args, kwargs))
    )
    monkeypatch.setattr(cli_main, "_stdio_is_tty", lambda: True)

    result = runner.invoke(app, ["chat", "--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert len(launched) == 1  # ушли в TUI

    # --plain: REPL читает stdin (CliRunner подаёт EOF — мгновенный выход).
    result = runner.invoke(app, ["chat", "--workspace", str(ws), "--plain"])
    assert result.exit_code == 0, result.output
    assert len(launched) == 1  # TUI не запускался
    assert "svarog chat" in result.output


def test_chat_without_tty_falls_back_to_plain(tmp_path, monkeypatch) -> None:
    from svarog_harness.cli import tui as tui_module

    ws = tmp_path / "ws"
    ws.mkdir()
    _write_chat_config(tmp_path, ws)
    monkeypatch.setenv("HOME", str(tmp_path))

    launched: list[tuple] = []
    monkeypatch.setattr(
        tui_module, "run_chat_tui", lambda *args, **kwargs: launched.append((args, kwargs))
    )

    result = runner.invoke(app, ["chat", "--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert launched == []  # CliRunner — не TTY: plain-REPL
