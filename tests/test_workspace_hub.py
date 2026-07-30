"""Тесты WorkspaceHub: мультиплекс GatewayService по корням (спека 2026-07-30)."""

from pathlib import Path

import pytest

from svarog_harness.config.loader import load_config
from svarog_harness.gateway.hub import RootGoneError, RootPathError, WorkspaceHub
from svarog_harness.gateway.roots import WorkspaceRootsRegistry


def _write_root(root: Path, db: Path) -> None:
    """Минимальный конфиг корня; db_path — вне корня, чтобы пережить rmdir."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {db}\n",
        encoding="utf-8",
    )


@pytest.fixture()
def hub(tmp_path: Path) -> WorkspaceHub:
    default_root = tmp_path / "default"
    _write_root(default_root, tmp_path / "default.db")
    cfg = load_config(project_dir=default_root)
    registry = WorkspaceRootsRegistry(tmp_path / "roots.json")
    return WorkspaceHub(cfg, default_root, registry=registry)


def test_default_root_service_reuses_base_cfg(hub: WorkspaceHub, tmp_path: Path) -> None:
    svc = hub.service_for(tmp_path / "default")
    assert svc.cfg is hub.base_cfg  # без повторного load_config
    assert svc is hub.service_for(tmp_path / "default")  # кэш


def test_service_for_loads_root_config(hub: WorkspaceHub, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    svc = hub.service_for(other)
    assert svc.workspace == other.resolve()
    assert svc.cfg is not hub.base_cfg
    assert svc is hub.service_for(other)  # кэш по resolved-пути


def test_service_for_rejects_bad_paths(hub: WorkspaceHub, tmp_path: Path) -> None:
    with pytest.raises(RootPathError):
        hub.service_for(tmp_path / "нет-такого")
    as_file = tmp_path / "файл.txt"
    as_file.write_text("x", encoding="utf-8")
    with pytest.raises(RootPathError):
        hub.service_for(as_file)


def test_route_by_session_and_miss(hub: WorkspaceHub, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    hub.registry.record_session("s-other", other)
    assert hub.route(session_id="s-other").workspace == other.resolve()
    # Промах реестра (сессия до фичи) → сервис default_root.
    assert hub.route(session_id="s-старая").workspace == (tmp_path / "default").resolve()
    assert hub.route().workspace == (tmp_path / "default").resolve()


def test_route_gone_root_is_410(hub: WorkspaceHub, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    hub.registry.record_session("s1", other)
    (other / "svarog.yaml").unlink()
    other.rmdir()
    with pytest.raises(RootGoneError):
        hub.route(session_id="s1")


def test_route_by_header_root(hub: WorkspaceHub, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    assert hub.route(root=str(other)).workspace == other.resolve()
    with pytest.raises(RootPathError):
        hub.route(root=str(tmp_path / "нет"))


def test_authenticate_bearer(hub: WorkspaceHub) -> None:
    assert hub.authenticate(None) is not None  # токен не настроен — открытый режим
    hub.bearer_token = "секрет"
    assert hub.authenticate(None) is None
    assert hub.authenticate("Bearer не-тот") is None
    assert hub.authenticate("Bearer секрет") is not None
    assert hub.authenticate(None, query_token="секрет") is not None
