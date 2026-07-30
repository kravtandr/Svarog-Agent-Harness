"""Тесты реестра корней workspace-сессий (спека 2026-07-30)."""

from pathlib import Path

from svarog_harness.gateway.roots import WorkspaceRootsRegistry


def test_records_and_orders_roots(tmp_path: Path) -> None:
    reg = WorkspaceRootsRegistry(tmp_path / "reg.json")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    reg.record_session("s1", a)
    reg.record_session("s2", b)
    assert [root for root, _ in reg.roots()] == [b, a]  # свежие сверху
    assert reg.root_of_session("s1") == a
    assert reg.root_of_session("нет-такой") is None


def test_records_runs(tmp_path: Path) -> None:
    reg = WorkspaceRootsRegistry(tmp_path / "reg.json")
    root = tmp_path / "w"
    root.mkdir()
    reg.record_run("r1", root)
    assert reg.root_of_run("r1") == root
    assert reg.roots_with_runs() == {root}


def test_tolerates_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "reg.json"
    path.write_text("{оборванный json", encoding="utf-8")
    reg = WorkspaceRootsRegistry(path)
    assert reg.roots() == []
    assert reg.root_of_session("s") is None
    root = tmp_path / "w"
    root.mkdir()
    reg.record_session("s", root)  # запись лечит файл
    assert reg.root_of_session("s") == root


def test_prunes_dead_roots_on_write(tmp_path: Path) -> None:
    reg = WorkspaceRootsRegistry(tmp_path / "reg.json")
    dead, alive = tmp_path / "dead", tmp_path / "alive"
    dead.mkdir()
    alive.mkdir()
    reg.record_session("s1", dead)
    dead.rmdir()
    reg.record_session("s2", alive)  # ленивая чистка при записи
    assert [root for root, _ in reg.roots()] == [alive]
    # Карта сессий не чистится: папка может вернуться (реестр — кэш).
    assert reg.root_of_session("s1") == dead


def test_missing_file_is_empty(tmp_path: Path) -> None:
    reg = WorkspaceRootsRegistry(tmp_path / "нет" / "reg.json")
    assert reg.roots() == []
    assert reg.roots_with_runs() == set()
