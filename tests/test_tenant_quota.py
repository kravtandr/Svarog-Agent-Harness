"""Фаза 3: квоты тенанта (ADR-0014) — лимиты и enforcement в gateway."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import QuotaConfig, SvarogConfig, TenantRole
from svarog_harness.gateway import TenantHub
from svarog_harness.gateway.api import create_app
from svarog_harness.tenant import (
    QuotaExceededError,
    QuotaUsage,
    TenantRegistry,
    check_quota,
    effective_quota,
)
from svarog_harness.tenant.models import TenantContext, TenantRecord


def _cfg(tmp_path: Path, quota_yaml: str = "") -> SvarogConfig:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n  default: local\n  providers:\n    local:\n"
        "      base_url: http://localhost:9/v1\n      model: fake\n"
        f"tenancy:\n  enabled: true\n  home_root: {tmp_path / 'homes'}\n{quota_yaml}",
        encoding="utf-8",
    )
    return load_config(project_dir=ws)


# --- unit ---------------------------------------------------------------------


def test_check_quota_limits() -> None:
    q = QuotaConfig(max_concurrent_runs=2, max_total_cost_usd=1.0, max_total_tokens=100)
    check_quota(QuotaUsage(1, 0.5, 50), q)  # под лимитами — ок
    with pytest.raises(QuotaExceededError, match="одновременных"):
        check_quota(QuotaUsage(2, 0.0, 0), q)
    with pytest.raises(QuotaExceededError, match="стоимости"):
        check_quota(QuotaUsage(0, 1.0, 0), q)
    with pytest.raises(QuotaExceededError, match="токен"):
        check_quota(QuotaUsage(0, 0.0, 100), q)


def test_zero_means_unlimited() -> None:
    check_quota(QuotaUsage(999, 999.0, 10**9), QuotaConfig())  # всё 0 — без лимитов


def test_effective_quota_override() -> None:
    default = QuotaConfig(max_concurrent_runs=5)
    rec = TenantRecord("alice", TenantRole.STANDARD, "", quotas={"max_concurrent_runs": 1})
    assert effective_quota(default, rec).max_concurrent_runs == 1
    assert effective_quota(default, None).max_concurrent_runs == 5  # нет override


# --- enforcement в gateway ----------------------------------------------------


def _hub(tmp_path: Path, quota_yaml: str = "") -> TenantHub:
    reg = TenantRegistry(tmp_path / "tenants.json")
    reg.create("alice", TenantRole.STANDARD)
    reg.add_principal("alice", "gateway:alice-tok")
    return TenantHub(_cfg(tmp_path, quota_yaml), reg)


def test_rest_429_when_quota_exceeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub = _hub(tmp_path, "  default_quota:\n    max_concurrent_runs: 1\n")
    svc = hub.service_for(TenantContext("alice", TenantRole.STANDARD))

    async def fake_usage() -> QuotaUsage:
        return QuotaUsage(active_runs=1, total_cost_usd=0.0, total_tokens=0)

    monkeypatch.setattr(svc, "usage", fake_usage)  # как будто уже 1 активный run
    client = TestClient(create_app(hub=hub))
    resp = client.post("/runs", json={"task": "x"}, headers={"Authorization": "Bearer alice-tok"})
    assert resp.status_code == 429
    assert "одновременных" in resp.json()["detail"]


def test_no_quota_allows_run(tmp_path: Path) -> None:
    # Без default_quota (все 0) guard не мешает; usage по пустой БД = 0.
    hub = _hub(tmp_path)
    svc = hub.service_for(TenantContext("alice", TenantRole.STANDARD))
    assert svc.quota_guard is not None
