"""Фаза 2: TenantHub + per-tenant auth в gateway (ADR-0014).

Проверяет резолвинг per-tenant токена в тенанта, изоляцию сервисов (свои
db_path/workspace под home) и кросс-тенантные границы на REST-слое.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import SvarogConfig, TenantRole
from svarog_harness.gateway import TenantHub
from svarog_harness.gateway.api import create_app
from svarog_harness.runtime import orchestrator
from svarog_harness.runtime.orchestrator import TaskRunner
from svarog_harness.sandbox import SandboxError
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
    # user_config_path -> несуществующий файл: тест должен быть герметичен и не
    # зависеть от ~/.svarog/svarog.yaml разработчика (иначе executor/sandbox
    # оттуда просачивается в merge и ломает local-trusted-сценарии).
    return load_config(project_dir=ws, user_config_path=tmp_path / "no-user.yaml")


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


# --- run_index + resume-роутинг ----------------------------------------------


def test_run_index_callback_records(tmp_path: Path) -> None:
    hub, reg = _hub(tmp_path)
    svc = hub.service_for(TenantContext("alice", TenantRole.STANDARD))
    assert svc.on_run_created is not None
    svc.on_run_created("run-1")  # эмулируем on_run_started
    assert reg.tenant_of_run("run-1") == "alice"


def test_active_tenant_ids_from_run_index(tmp_path: Path) -> None:
    _, reg = _hub(tmp_path)
    reg.record_run("r1", "alice")
    reg.record_run("r2", "bob")
    reg.record_run("r3", "alice")
    assert set(reg.active_tenant_ids()) == {"alice", "bob"}


async def test_hub_resume_unknown_run_returns_false(tmp_path: Path) -> None:
    hub, _ = _hub(tmp_path)
    assert await hub.resume_run("no-such-run") is False


# --- fail-closed (ADR-0013) ---------------------------------------------------


def test_fail_closed_standard_without_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, _ = _hub(tmp_path)
    alice = hub.service_for(TenantContext("alice", TenantRole.STANDARD))
    runner = TaskRunner(alice.cfg, alice.workspace)  # cfg.sandbox.type == docker
    monkeypatch.setattr(orchestrator, "find_docker", lambda: None)
    with pytest.raises(SandboxError):
        runner.assert_sandbox_available()
    monkeypatch.setattr(orchestrator, "find_docker", lambda: "/usr/bin/docker")
    runner.assert_sandbox_available()  # docker есть — не бросает


def test_superuser_local_trusted_needs_no_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, _ = _hub(tmp_path)
    bob = hub.service_for(TenantContext("bob", TenantRole.SUPERUSER))
    runner = TaskRunner(bob.cfg, bob.workspace)  # local-trusted
    monkeypatch.setattr(orchestrator, "find_docker", lambda: None)
    runner.assert_sandbox_available()  # local-trusted не требует docker
