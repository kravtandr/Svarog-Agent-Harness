"""Тесты WorkspaceHub: мультиплекс GatewayService по корням (спека 2026-07-30)."""

import asyncio
import json
from pathlib import Path

import pytest

from svarog_harness.config.loader import load_config
from svarog_harness.gateway.hub import (
    RootConfigError,
    RootGoneError,
    RootPathError,
    WorkspaceHub,
)
from svarog_harness.gateway.models import CreateSessionRequest
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


def test_service_for_bad_config_is_root_path_error(hub: WorkspaceHub, tmp_path: Path) -> None:
    """F1: ConfigError из load_config оборачивается в RootConfigError (подкласс RootPathError)."""
    broken = tmp_path / "битый"
    broken.mkdir()
    (broken / "svarog.yaml").write_text("models: [оборванный", encoding="utf-8")
    with pytest.raises(RootConfigError):
        hub.service_for(broken)
    with pytest.raises(RootPathError):  # существующие обработчики ловят по базовому классу
        hub.service_for(broken)


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


def test_route_session_id_wins_over_header_root(hub: WorkspaceHub, tmp_path: Path) -> None:
    """F6: сессионные запросы маршрутизируются по id, заголовок для них не главный."""
    by_id = tmp_path / "by-id"
    _write_root(by_id, tmp_path / "by-id.db")
    by_header = tmp_path / "by-header"
    _write_root(by_header, tmp_path / "by-header.db")
    hub.registry.record_session("s1", by_id)
    assert hub.route(session_id="s1", root=str(by_header)).workspace == by_id.resolve()


def test_authenticate_bearer(hub: WorkspaceHub) -> None:
    assert hub.authenticate(None) is not None  # токен не настроен — открытый режим
    hub.bearer_token = "секрет"
    assert hub.authenticate(None) is None
    assert hub.authenticate("Bearer не-тот") is None
    assert hub.authenticate("Bearer секрет") is not None
    assert hub.authenticate(None, query_token="секрет") is not None


def test_list_sessions_aggregates_and_dedups(hub: WorkspaceHub, tmp_path: Path) -> None:
    other = tmp_path / "other"
    # Общая БД с default-корнем — случай «корень без своего db_path»:
    # одна сессия видна из двух сервисов, список не должен двоиться.
    _write_root(other, tmp_path / "default.db")
    default_svc = hub.service_for(tmp_path / "default")
    other_svc = hub.service_for(other)

    async def scenario() -> list:
        await default_svc.create_session(title="в default")
        await other_svc.create_session(title="в other")
        return await hub.list_sessions()

    listed = asyncio.run(scenario())
    assert [s.title for s in listed] == ["в other", "в default"]  # свежие сверху, без дублей
    assert listed[0].workspace == str(other.resolve())


def test_list_sessions_skips_gone_roots(hub: WorkspaceHub, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    svc = hub.service_for(other)
    asyncio.run(svc.create_session(title="обречённая"))
    (other / "svarog.yaml").unlink()
    other.rmdir()
    hub._services.pop(other.resolve())  # рестарт serve: сервис не материализован
    titles = [s.title for s in asyncio.run(hub.list_sessions())]
    assert "обречённая" not in titles  # корень пропущен, а не 500


def test_list_fs_dirs_only_hidden_filtered(hub: WorkspaceHub, tmp_path: Path) -> None:
    base = tmp_path / "обзор"
    (base / "видимая").mkdir(parents=True)
    (base / ".скрытая").mkdir()
    (base / "файл.txt").write_text("x", encoding="utf-8")
    listing = hub.list_fs(str(base))
    assert [e.name for e in listing.entries] == ["видимая"]
    assert listing.path == str(base.resolve())
    assert listing.parent == str(base.resolve().parent)
    with pytest.raises(RootPathError):
        hub.list_fs(str(base / "нет-такого"))


def test_recent_roots_marks_missing(hub: WorkspaceHub, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    hub.registry.record_session("s1", other)
    (other / "svarog.yaml").unlink()
    other.rmdir()
    recents = hub.recent_roots()
    assert [(r.path, r.exists) for r in recents] == [(str(other), False)]


def test_recent_roots_tolerates_broken_last_used(hub: WorkspaceHub, tmp_path: Path) -> None:
    """F5: битый last_used в roots.json — запись пропущена, не 500 на /fs/recent."""
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    hub.registry.record_session("s1", other)
    data = json.loads(hub.registry.path.read_text(encoding="utf-8"))
    for key in data["roots"]:
        data["roots"][key] = "не-дата"
    hub.registry.path.write_text(json.dumps(data), encoding="utf-8")
    assert hub.recent_roots() == []  # реестр — кэш, битая запись не повод падать


def test_create_requests_path_exclusive() -> None:
    with pytest.raises(ValueError, match="path"):
        CreateSessionRequest(title="x", path="/tmp", workspace="named")
    assert CreateSessionRequest(title="x", path="/tmp").path == "/tmp"
