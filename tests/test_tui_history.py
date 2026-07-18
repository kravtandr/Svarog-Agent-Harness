"""InputHistory: загрузка, дозапись, дедупликация, кап, навигация курсором."""

from pathlib import Path

from svarog_harness.cli.tui.history import InputHistory


def _history(tmp_path: Path, **kwargs: int) -> InputHistory:
    return InputHistory(tmp_path / "chat_history", **kwargs)


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    assert _history(tmp_path).entries == []


def test_append_persists_and_reloads(tmp_path: Path) -> None:
    history = _history(tmp_path)
    history.append("первое")
    history.append("второе")
    assert _history(tmp_path).entries == ["первое", "второе"]


def test_append_skips_blank_and_duplicate_of_last(tmp_path: Path) -> None:
    history = _history(tmp_path)
    history.append("привет")
    history.append("  ")
    history.append("привет")
    assert history.entries == ["привет"]


def test_limit_trims_oldest(tmp_path: Path) -> None:
    history = _history(tmp_path, limit=3)
    for i in range(5):
        history.append(f"msg{i}")
    assert history.entries == ["msg2", "msg3", "msg4"]
    assert _history(tmp_path, limit=3).entries == ["msg2", "msg3", "msg4"]


def test_prev_next_navigation_returns_draft(tmp_path: Path) -> None:
    history = _history(tmp_path)
    history.append("a")
    history.append("b")
    assert history.prev("черновик") == "b"
    assert history.prev() == "a"
    assert history.prev() == "a"  # упор в начало
    assert history.next() == "b"
    assert history.next() == "черновик"  # выход из навигации возвращает ввод
    assert history.next() is None


def test_prev_on_empty_history(tmp_path: Path) -> None:
    assert _history(tmp_path).prev("x") is None
