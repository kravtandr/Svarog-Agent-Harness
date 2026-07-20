"""Презентация chat: tool-карточки, status snapshot, welcome, синий chrome."""

from pathlib import Path

import pytest
from rich.console import Console

from svarog_harness.cli.chat_display import (
    ACCENT,
    ChatStatusView,
    chat_status_view,
    format_tool_call,
    input_separator,
    status_summary,
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


def _minimal_yaml(tmp_path: Path, extra: str = "") -> str:
    return (
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n"
        f"{extra}"
    )


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


def test_status_view_native_lists_modes_and_default_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "svarog_harness.cli.chat_display._adapter_available",
        lambda name: False,
    )
    cfg = _cfg(tmp_path, _minimal_yaml(tmp_path, "sandbox:\n  type: local-trusted\n"))
    view = chat_status_view(cfg, AutonomyMode.YOLO)
    assert view.active_executor == "native/local"
    assert "native/local" in view.executors
    assert "external/claude-code" not in view.executors
    assert view.modes == ("локальный loop", "cloud-агент")
    assert view.active_mode == "локальный loop"
    assert view.policy_profile == "default"
    assert view.autonomy is AutonomyMode.YOLO


def test_status_view_includes_detected_and_configured_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "svarog_harness.cli.chat_display._adapter_available",
        lambda name: name == "codex",
    )
    cfg = _cfg(
        tmp_path,
        _minimal_yaml(
            tmp_path,
            "sandbox:\n  type: docker\n"
            "executor:\n"
            "  type: external\n"
            "  external:\n"
            "    adapter: claude-code\n"
            "    image: svarog/claude:test\n",
        ),
    )
    view = chat_status_view(cfg, AutonomyMode.AUTO)
    assert view.active_executor == "external/claude-code"
    assert view.active_mode == "cloud-агент"
    assert "native/docker" in view.executors
    assert "external/claude-code" in view.executors  # из конфига
    assert "external/codex" in view.executors  # which
    assert "external/opencode" not in view.executors
    assert view.policy_profile == "default"


def test_status_view_policy_profile_matches_autonomy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "svarog_harness.cli.chat_display._adapter_available",
        lambda _name: False,
    )
    cfg = _cfg(
        tmp_path,
        _minimal_yaml(
            tmp_path,
            "sandbox:\n  type: docker\npolicies:\n  profiles:\n    yolo:\n      notify: ['bash']\n",
        ),
    )
    view = chat_status_view(cfg, AutonomyMode.YOLO)
    assert view.policy_profile == "yolo"
    assert chat_status_view(cfg, AutonomyMode.AUTO).policy_profile == "default"


def test_status_summary_is_compact() -> None:
    view = ChatStatusView(
        autonomy=AutonomyMode.YOLO,
        executors=("native/docker", "external/claude-code"),
        active_executor="native/docker",
        modes=("локальный loop", "cloud-агент"),
        active_mode="локальный loop",
        policy_profile="default",
    )
    assert status_summary(view) == "▶▶ yolo · native/docker · local · default"


def test_welcome_layout_table_and_separator() -> None:
    assert ACCENT == "dodger_blue2"
    view = ChatStatusView(
        autonomy=AutonomyMode.YOLO,
        executors=("native/docker", "external/opencode"),
        active_executor="external/opencode",
        modes=("локальный loop", "cloud-агент"),
        active_mode="cloud-агент",
        policy_profile="default",
    )
    panel = welcome_panel(Path("/tmp/demo"), view, model="openai/gpt")
    out = _plain(panel)
    assert "Svarog chat" in out
    assert "Welcome" in out
    assert "Tips for getting started" in out
    assert "executors" in out
    assert "native/docker" in out and "external/opencode" in out
    assert "mode" in out
    assert "локальный loop" in out and "cloud-агент" in out
    assert "policies" in out
    assert "yolo" in out and "default" in out
    assert "openai/gpt" in out
    assert "/help · /new" not in _plain(input_separator())


def test_policies_text_default_profile_is_dim() -> None:
    from svarog_harness.cli.chat_display import _policies_text

    default_view = ChatStatusView(
        autonomy=AutonomyMode.YOLO,
        executors=("native/docker",),
        active_executor="native/docker",
        modes=("локальный loop", "cloud-агент"),
        active_mode="локальный loop",
        policy_profile="default",
    )
    text = _policies_text(default_view)
    assert text.plain == "yolo / default"
    by_style = {text.plain[s.start : s.end]: str(s.style) for s in text.spans}
    assert by_style["yolo"] == ACCENT
    assert by_style["default"] == "dim"

    named = ChatStatusView(
        autonomy=AutonomyMode.YOLO,
        executors=("native/docker",),
        active_executor="native/docker",
        modes=("локальный loop", "cloud-агент"),
        active_mode="локальный loop",
        policy_profile="yolo",
    )
    named_text = _policies_text(named)
    assert named_text.plain == "yolo / yolo"
    named_styles = [
        str(s.style) for s in named_text.spans if named_text.plain[s.start : s.end] == "yolo"
    ]
    assert named_styles == [ACCENT, ACCENT]
