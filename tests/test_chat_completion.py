"""CompletionMode IDLE/SLASH/AT — паттерн qwen-code для подсказок `/` и `@`."""

from pathlib import Path

from svarog_harness.cli.chat_completion import (
    CompletionMode,
    at_suggestions,
    detect_completion,
    list_workspace_files,
    slash_suggestions,
)


def test_detect_idle_for_plain_text() -> None:
    assert detect_completion("привет").mode is CompletionMode.IDLE
    assert detect_completion("").mode is CompletionMode.IDLE
    assert detect_completion("/help extra").mode is CompletionMode.IDLE


def test_detect_slash_at_line_start() -> None:
    q = detect_completion("/")
    assert q.mode is CompletionMode.SLASH and q.token == "/"
    q = detect_completion("/he")
    assert q.mode is CompletionMode.SLASH and q.token == "/he"


def test_detect_at_mid_line() -> None:
    q = detect_completion("смотри @src/a")
    assert q.mode is CompletionMode.AT and q.token == "@src/a"
    q = detect_completion("@")
    assert q.mode is CompletionMode.AT and q.token == "@"


def test_slash_suggestions_filter_and_describe() -> None:
    items = slash_suggestions("/he")
    assert len(items) == 1 and items[0].value == "/help"
    assert items[0].description
    assert {s.value for s in slash_suggestions("/")} >= {"/help", "/quit", "/sessions"}


def test_at_suggestions_from_workspace(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("", encoding="utf-8")
    files = list_workspace_files(tmp_path)
    assert "src/main.py" in files and "README.md" in files
    assert not any(f.startswith(".git") for f in files)
    hits = at_suggestions(tmp_path, "@main")
    assert any(s.value == "@src/main.py" for s in hits)
    assert at_suggestions(tmp_path, "main") == []
