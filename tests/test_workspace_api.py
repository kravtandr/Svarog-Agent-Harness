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


def test_hub_registry_survives_restart(tmp_path: Path) -> None:
    """Рестарт serve: новый хаб с тем же реестром маршрутизирует старую сессию."""
    default_root = tmp_path / "default"
    _write_root(default_root, tmp_path / "default.db")
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    registry_path = tmp_path / "roots.json"

    def make_client() -> TestClient:
        hub = WorkspaceHub(
            load_config(project_dir=default_root),
            default_root,
            registry=WorkspaceRootsRegistry(registry_path),
        )
        return TestClient(create_app(resolver=hub))

    session_id = make_client().post("/sessions", json={"path": str(other)}).json()["session_id"]
    reborn = make_client()  # «рестарт»: свежий хаб, тот же файл реестра
    thread = reborn.get(f"/sessions/{session_id}/messages")
    assert thread.status_code == 200


def test_create_session_bad_root_config_is_422(client: TestClient, tmp_path: Path) -> None:
    """F1: битый svarog.yaml корня — 422, не 500 (ConfigError нигде не перехвачен)."""
    broken = tmp_path / "битый"
    broken.mkdir()
    (broken / "svarog.yaml").write_text("models: [оборванный", encoding="utf-8")
    resp = client.post("/sessions", json={"title": "x", "path": str(broken)})
    assert resp.status_code == 422


def test_list_sessions_skips_root_with_broken_config_after_restart(tmp_path: Path) -> None:
    """F1(б): сессия default-корня видна, даже если чужой корень сломан после рестарта."""
    default_root = tmp_path / "default"
    _write_root(default_root, tmp_path / "default.db")
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    registry_path = tmp_path / "roots.json"

    def make_client() -> TestClient:
        hub = WorkspaceHub(
            load_config(project_dir=default_root),
            default_root,
            registry=WorkspaceRootsRegistry(registry_path),
        )
        return TestClient(create_app(resolver=hub))

    first = make_client()
    default_session = first.post("/sessions", json={"title": "в default"}).json()["session_id"]
    other_session = first.post("/sessions", json={"path": str(other)}).json()["session_id"]

    # Корень ломается уже после того, как сессия в нём создана и записана в
    # реестр; хаб пересоздан («рестарт serve»), чужой сервис не материализован.
    (other / "svarog.yaml").write_text("models: [оборванный", encoding="utf-8")
    reborn = make_client()

    listed = reborn.get("/sessions")
    assert listed.status_code == 200
    ids = [s["session_id"] for s in listed.json()]
    assert default_session in ids
    assert other_session not in ids  # битый корень пропущен, а не 500

    assert reborn.get(f"/sessions/{other_session}/messages").status_code == 422


def test_delete_zombie_session_with_gone_root_succeeds(tmp_path: Path) -> None:
    """F2: удалённый корень с общей БД default-корня — DELETE всё равно работает."""
    default_root = tmp_path / "default"
    _write_root(default_root, tmp_path / "default.db")
    other = tmp_path / "other"
    # Общая БД с default-корнем (как в test_list_sessions_aggregates_and_dedups):
    # строка сессии переживает исчезновение папки other.
    _write_root(other, tmp_path / "default.db")
    hub = WorkspaceHub(
        load_config(project_dir=default_root),
        default_root,
        registry=WorkspaceRootsRegistry(tmp_path / "roots.json"),
    )
    client = TestClient(create_app(resolver=hub))

    session_id = client.post("/sessions", json={"path": str(other)}).json()["session_id"]
    shutil.rmtree(other)

    resp = client.delete(f"/sessions/{session_id}")
    assert resp.status_code == 204
    remaining = [s["session_id"] for s in client.get("/sessions").json()]
    assert session_id not in remaining


def test_created_session_reports_root(client: TestClient, tmp_path: Path) -> None:
    """F4: GET /sessions отдаёт root сервиса, обработавшего path-сессию."""
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    session_id = client.post("/sessions", json={"path": str(other)}).json()["session_id"]
    listed = client.get("/sessions").json()
    [row] = [s for s in listed if s["session_id"] == session_id]
    assert row["root"] == str(other.resolve())


def _write_overlapping_root(root: Path, *, sandbox: str) -> None:
    """Корень, у которого control-plane лежит ВНУТРИ workspace (ADR-0015 §0.3)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        f"sandbox:\n  type: {sandbox}\n"
        f"storage:\n  db_path: {root / '.svarog' / 'svarog.db'}\n",
        encoding="utf-8",
    )


def test_fs_inspect_reports_blocking_overlap(client: TestClient, tmp_path: Path) -> None:
    """Пересечение с control-plane видно ДО создания чата (диалог в пикере)."""
    overlapped = tmp_path / "overlapped"
    _write_overlapping_root(overlapped, sandbox="docker")
    resp = client.get("/fs/inspect", params={"path": str(overlapped)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["blocking"] is True
    assert any("storage.db_path" in warning for warning in body["overlap_warnings"])


def test_fs_inspect_local_trusted_not_blocking(client: TestClient, tmp_path: Path) -> None:
    """В local-trusted пересечение — документированный trade-off: без диалога."""
    overlapped = tmp_path / "trusted"
    _write_overlapping_root(overlapped, sandbox="local-trusted")
    body = client.get("/fs/inspect", params={"path": str(overlapped)}).json()
    assert body["overlap_warnings"] != []
    assert body["blocking"] is False


def test_fs_inspect_clean_root(client: TestClient, tmp_path: Path) -> None:
    other = tmp_path / "чистый"
    _write_root(other, tmp_path / "чистый.db")
    body = client.get("/fs/inspect", params={"path": str(other)}).json()
    assert body == {"path": str(other.resolve()), "overlap_warnings": [], "blocking": False}


def test_accept_overlap_marks_session_and_runner(tmp_path: Path) -> None:
    """Согласие из пикера доезжает до раннера: runs сессии идут с allow_layout_overlap."""
    import asyncio

    from svarog_harness.trace.lookup import find_session_by_prefix

    overlapped = tmp_path / "overlapped"
    _write_overlapping_root(overlapped, sandbox="docker")
    svc = GatewayService(load_config(project_dir=overlapped), overlapped)

    async def scenario() -> tuple[bool, bool]:
        view = await svc.create_session(title="x", accept_overlap=True)

        async def read(db):  # type: ignore[no-untyped-def]
            return await find_session_by_prefix(db, view.session_id)

        session = await svc._read(read)
        allow = bool((session.meta or {}).get("allow_overlap"))
        # Сборка раннера ровно как в send_message: по meta сессии. Общий
        # self._runner (без флага) переиспользоваться не должен.
        built = svc._runner_for(svc.workspace, allow_overlap=allow)
        return allow, built is not svc._runner and built._allow_layout_overlap

    allow, runner_flagged = asyncio.run(scenario())
    assert allow is True
    assert runner_flagged is True


def test_accept_overlap_rejected_without_hub(tmp_path: Path) -> None:
    """Fail-closed: вне single-tenant согласие не принимается."""
    root = tmp_path / "root"
    _write_root(root, tmp_path / "root.db")
    service = GatewayService(load_config(project_dir=root), root)
    plain = TestClient(create_app(service))
    resp = plain.post("/sessions", json={"title": "x", "accept_overlap": True})
    assert resp.status_code == 422


def test_global_mcp_reaches_every_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP подключается к самому Сварогу: добавленный в одном корне виден во всех.

    Хаб кеширует сервис на корень, и каждый держит свой снимок конфига. Правка
    общего пользовательского слоя обязана дойти до всех — иначе «глобально»
    работает только там, где нажали кнопку, а в соседнем корне сервера нет до
    перезапуска (найдено живой проверкой 2026-08-06).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    default_root = tmp_path / "default"
    _write_root(default_root, tmp_path / "default.db")
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")

    hub = WorkspaceHub(
        load_config(project_dir=default_root),
        default_root,
        registry=WorkspaceRootsRegistry(tmp_path / "roots.json"),
    )
    client = TestClient(create_app(resolver=hub))

    # Сервис соседнего корня создаётся ДО правки — как это и происходит в
    # жизни: сайдбар опрашивает все корни при загрузке страницы.
    assert client.get("/mcp", headers={"X-Svarog-Root": str(other)}).json() == []

    added = client.post(
        "/mcp",
        json={"name": "memory", "command": "npx", "args": [], "risk": "low"},
        headers={"X-Svarog-Root": str(default_root)},
    )
    assert added.status_code == 200, added.text
    assert added.json()["path"] == str(tmp_path / ".svarog" / "svarog.yaml")

    seen = client.get("/mcp", headers={"X-Svarog-Root": str(other)}).json()
    assert [s["name"] for s in seen] == ["memory"]
    assert seen[0]["scope"] == "user"
