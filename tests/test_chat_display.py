"""Презентация chat: tool-карточки, executor, welcome, синий chrome."""

from pathlib import Path

import pytest
from rich.console import Console

from svarog_harness.cli.chat_display import (
    ACCENT,
    ExecutorView,
    executor_view,
    format_tool_call,
    input_separator,
    welcome_panel,
)
from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import AutonomyMode, SvarogConfig


def _plain(renderable: object) -> str:
    console = Console(record=True, width=100, force_terminal=False)
    console.print(renderable)
    return console.export_text()


def _cfg(tmp_path: Path, body: str) -> SvarogConfig:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(body, encoding="utf-8")
    return load_config(project_dir=ws)


def test_write_hides_content_shows_path_and_size() -> None:
    html = "<html>" + ("x" * 2000) + "</html>"
    out = _plain(format_tool_call("write", {"filePath": "/workspace/index.html", "content": html}))
    assert "Write" in out
    assert "/workspace/index.html" in out
    assert "KB" in out or "B" in out
    assert html not in out
    assert "content" not in out


def test_write_file_native_name() -> None:
    out = _plain(format_tool_call("write_file", {"path": "src/a.py", "content": "print(1)\n"}))
    assert "Write" in out and "src/a.py" in out
    assert "print(1)" not in out


def test_bash_shows_command() -> None:
    out = _plain(format_tool_call("Bash", {"command": "ls -la /tmp"}))
    assert "$" in out
    assert "ls -la /tmp" in out


def test_read_and_edit() -> None:
    assert "Read" in _plain(format_tool_call("Read", {"file_path": "a.txt"}))
    edit = format_tool_call("edit_file", {"path": "a.txt", "old_string": "x" * 500})
    assert "Edit" in _plain(edit)
    assert "x" * 20 not in _plain(edit)


def test_unknown_tool_summarizes_without_bulk() -> None:
    out = _plain(
        format_tool_call(
            "custom_tool",
            {"path": "x", "content": "secret-body", "flag": True},
        )
    )
    assert "custom_tool" in out
    assert "secret-body" not in out
    assert "content=<12 chars>" in out or "content=" in out


def test_executor_view_native_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(
        tmp_path,
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
    )
    view = executor_view(cfg)
    assert view == ExecutorView(kind="native", detail="local", role="локальный loop")


def test_executor_view_external_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(
        tmp_path,
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: docker\n"
        "executor:\n"
        "  type: external\n"
        "  external:\n"
        "    adapter: claude-code\n"
        "    image: svarog/claude:test\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
    )
    view = executor_view(cfg)
    assert view.kind == "external" and view.detail == "claude-code"
    assert view.role == "cloud-агент"


def test_welcome_layout_and_separator() -> None:
    assert ACCENT == "dodger_blue2"
    view = ExecutorView(kind="external", detail="opencode", role="cloud-агент")
    panel = welcome_panel(Path("/tmp/demo"), AutonomyMode.YOLO, view, model="openai/gpt")
    out = _plain(panel)
    assert "Svarog chat" in out
    assert "Welcome" in out
    assert "Tips for getting started" in out
    assert "opencode" in out and "cloud-агент" in out
    assert "openai/gpt" in out
    # Постоянного списка /help · /new у промпта больше нет — только полоса.
    assert "/help · /new" not in _plain(input_separator())
