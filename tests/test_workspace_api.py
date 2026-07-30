"""API-тесты выбора рабочей папки (спека 2026-07-30): path, маршрутизация, /fs."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app
from svarog_harness.gateway.hub import WorkspaceHub
from svarog_harness.gateway.roots import WorkspaceRootsRegistry


def _write_root(root: Path, db: Path) -> None:
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
def client(tmp_path: Path) -> TestClient:
    default_root = tmp_path / "default"
    _write_root(default_root, tmp_path / "default.db")
    hub = WorkspaceHub(
        load_config(project_dir=default_root),
        default_root,
        registry=WorkspaceRootsRegistry(tmp_path / "roots.json"),
    )
    return TestClient(create_app(resolver=hub))


def test_create_session_with_path_routes_follow_ups(client: TestClient, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    created = client.post("/sessions", json={"title": "чат", "path": str(other)})
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    assert created.json()["workspace"] == str(other.resolve())
    # Follow-up маршрутизируется в сервис корня по session_id из пути URL.
    thread = client.get(f"/sessions/{session_id}/messages")
    assert thread.status_code == 200
    # Агрегированный список видит сессию чужого корня.
    listed = client.get("/sessions").json()
    assert [s["session_id"] for s in listed] == [session_id]
    assert listed[0]["workspace"] == str(other.resolve())


def test_create_session_path_errors(client: TestClient, tmp_path: Path) -> None:
    missing = client.post("/sessions", json={"title": "x", "path": str(tmp_path / "нет")})
    assert missing.status_code == 422
    both = client.post(
        "/sessions", json={"title": "x", "path": str(tmp_path), "workspace": "named"}
    )
    assert both.status_code == 422


def test_gone_root_is_410(client: TestClient, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    session_id = client.post("/sessions", json={"path": str(other)}).json()["session_id"]
    shutil.rmtree(other)
    assert client.get(f"/sessions/{session_id}/messages").status_code == 410


def test_fs_listing_and_recent(client: TestClient, tmp_path: Path) -> None:
    base = tmp_path / "обзор"
    (base / "внутри").mkdir(parents=True)
    (base / ".скрытая").mkdir()
    listing = client.get("/fs", params={"path": str(base)})
    assert listing.status_code == 200
    assert [e["name"] for e in listing.json()["entries"]] == ["внутри"]
    assert client.get("/fs", params={"path": str(base / "нет")}).status_code == 422
    other = tmp_path / "недавний"
    _write_root(other, tmp_path / "недавний.db")
    client.post("/sessions", json={"path": str(other)})
    recents = client.get("/fs/recent").json()
    assert str(other) in [r["path"] for r in recents]


def test_single_service_mode_has_no_fs_and_rejects_path(tmp_path: Path) -> None:
    """Без WorkspaceHub (multi-tenant и legacy-тесты) фичи не существует."""
    root = tmp_path / "root"
    _write_root(root, tmp_path / "root.db")
    service = GatewayService(load_config(project_dir=root), root)
    plain = TestClient(create_app(service))
    assert plain.get("/fs").status_code == 404
    assert plain.post("/sessions", json={"title": "x", "path": str(root)}).status_code == 422
