"""Фаза 2: TenantHub + per-tenant auth в gateway (ADR-0014).

Проверяет резолвинг per-tenant токена в тенанта, изоляцию сервисов (свои
db_path/workspace под home) и кросс-тенантные границы на REST-слое.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import SvarogConfig, TenantRole
from svarog_harness.gateway import TenantHub
from svarog_harness.gateway.api import create_app
from svarog_harness.tenant import TenantRegistry
from svarog_harness.tenant.models import TenantContext


def _base_cfg(tmp_path: Path) -> SvarogConfig:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake\n"
        "sandbox:\n  type: local-trusted\n"
        f"tenancy:\n  enabled: true\n  home_root: {tmp_path / 'homes'}\n",
        encoding="utf-8",
    )
    return load_config(project_dir=ws)


def _hub(tmp_path: Path) -> tuple[TenantHub, TenantRegistry]:
    reg = TenantRegistry(tmp_path / "tenants.json")
    reg.create("alice", TenantRole.STANDARD)
    reg.create("bob", TenantRole.SUPERUSER)
    reg.add_principal("alice", "gateway:alice-tok")
    reg.add_principal("bob", "gateway:bob-tok")
    return TenantHub(_base_cfg(tmp_path), reg), reg


# --- изоляция сервисов --------------------------------------------------------


def test_service_for_isolates_paths(tmp_path: Path) -> None:
    hub, _ = _hub(tmp_path)
    alice = hub.service_for(TenantContext("alice", TenantRole.STANDARD))
    bob = hub.service_for(TenantContext("bob", TenantRole.SUPERUSER))

    homes = (tmp_path / "homes").resolve()
    assert alice.cfg.storage.db_path == homes / "alice" / "svarog.db"
    assert bob.cfg.storage.db_path == homes / "bob" / "svarog.db"
    assert alice.workspace == homes / "alice" / "workspaces"
    assert alice.cfg.storage.db_path != bob.cfg.storage.db_path


def test_service_for_caches_instance(tmp_path: Path) -> None:
    hub, _ = _hub(tmp_path)
    ctx = TenantContext("alice", TenantRole.STANDARD)
    assert hub.service_for(ctx) is hub.service_for(ctx)


def test_standard_service_is_clamped(tmp_path: Path) -> None:
    hub, _ = _hub(tmp_path)
    # base sandbox = local-trusted, но роль standard клампит в docker (ADR-0013).
    alice = hub.service_for(TenantContext("alice", TenantRole.STANDARD))
    assert alice.cfg.sandbox.type == "docker"
    assert alice.cfg.secrets.env_fallback is False
    bob = hub.service_for(TenantContext("bob", TenantRole.SUPERUSER))
    assert bob.cfg.sandbox.type == "local-trusted"


# --- auth-резолвинг -----------------------------------------------------------


def test_authenticate_maps_token_to_tenant(tmp_path: Path) -> None:
    hub, _ = _hub(tmp_path)
    svc = hub.authenticate("Bearer alice-tok")
    assert svc is hub.service_for(TenantContext("alice", TenantRole.STANDARD))
    assert hub.authenticate("Bearer unknown") is None
    assert hub.authenticate(None) is None


# --- REST-границы -------------------------------------------------------------


def test_rest_per_tenant_auth(tmp_path: Path) -> None:
    hub, _ = _hub(tmp_path)
    client = TestClient(create_app(hub=hub))

    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/runs").status_code == 401
    assert client.get("/runs", headers={"Authorization": "Bearer nope"}).status_code == 401

    alice = {"Authorization": "Bearer alice-tok"}
    bob = {"Authorization": "Bearer bob-tok"}
    assert client.get("/runs", headers=alice).json() == []
    assert client.get("/runs", headers=bob).json() == []


def test_rest_cross_tenant_run_is_404(tmp_path: Path) -> None:
    # Каждый тенант видит только свою БД: чужой/несуществующий run → 404.
    hub, _ = _hub(tmp_path)
    client = TestClient(create_app(hub=hub))
    resp = client.get("/runs/deadbeef", headers={"Authorization": "Bearer alice-tok"})
    assert resp.status_code == 404


def test_ws_rejects_without_token(tmp_path: Path) -> None:
    hub, _ = _hub(tmp_path)
    client = TestClient(create_app(hub=hub))
    rejected = False
    try:
        with client.websocket_connect("/runs/deadbeef/events"):
            pass
    except Exception:
        rejected = True
    assert rejected
