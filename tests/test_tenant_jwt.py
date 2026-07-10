"""Фаза 3: JWT-бэкенд auth (ADR-0014) — HS256 verify + JwtResolver."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import SvarogConfig, TenantRole
from svarog_harness.gateway import JwtResolver, TenantHub, mint_tenant_jwt
from svarog_harness.gateway.api import create_app
from svarog_harness.gateway.jwt_auth import (
    InvalidTokenError,
    decode_hs256,
    encode_hs256,
)
from svarog_harness.tenant import TenantRegistry

_SECRET = "super-secret-signing-key"


def _cfg(tmp_path: Path) -> SvarogConfig:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n  default: local\n  providers:\n    local:\n"
        "      base_url: http://localhost:9/v1\n      model: fake\n"
        f"tenancy:\n  enabled: true\n  home_root: {tmp_path / 'homes'}\n",
        encoding="utf-8",
    )
    return load_config(project_dir=ws)


def _hub(tmp_path: Path) -> TenantHub:
    reg = TenantRegistry(tmp_path / "tenants.json")
    reg.create("alice", TenantRole.STANDARD)
    return TenantHub(_cfg(tmp_path), reg)


# --- HS256 verify -------------------------------------------------------------


def test_encode_decode_roundtrip() -> None:
    token = encode_hs256({"sub": "alice", "exp": time.time() + 60}, _SECRET)
    assert decode_hs256(token, _SECRET)["sub"] == "alice"


def test_rejects_wrong_secret() -> None:
    token = mint_tenant_jwt("alice", _SECRET)
    with pytest.raises(InvalidTokenError, match="подпись"):
        decode_hs256(token, "other-secret")


def test_rejects_tampered_payload() -> None:
    token = mint_tenant_jwt("alice", _SECRET)
    header, _payload, sig = token.split(".")
    forged = encode_hs256({"sub": "attacker", "exp": time.time() + 60}, _SECRET)
    tampered = f"{header}.{forged.split('.')[1]}.{sig}"
    with pytest.raises(InvalidTokenError):
        decode_hs256(tampered, _SECRET)


def test_rejects_expired() -> None:
    token = encode_hs256({"sub": "alice", "exp": time.time() - 1}, _SECRET)
    with pytest.raises(InvalidTokenError, match="истёк"):
        decode_hs256(token, _SECRET)


def test_rejects_malformed() -> None:
    with pytest.raises(InvalidTokenError):
        decode_hs256("not.a.jwt.at.all", _SECRET)


# --- JwtResolver --------------------------------------------------------------


def test_resolver_maps_valid_jwt_to_tenant(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    resolver = JwtResolver(hub, _SECRET)
    token = mint_tenant_jwt("alice", _SECRET)
    svc = resolver.authenticate(f"Bearer {token}")
    assert svc is not None
    assert svc.cfg.sandbox.type == "docker"  # роль standard из реестра → кламп


def test_resolver_role_comes_from_registry_not_claim(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    resolver = JwtResolver(hub, _SECRET)
    # claim пытается объявить superuser — резолвер обязан взять роль из реестра.
    forged = encode_hs256({"sub": "alice", "role": "superuser", "exp": time.time() + 60}, _SECRET)
    svc = resolver.authenticate(f"Bearer {forged}")
    assert svc is not None
    assert svc.cfg.sandbox.type == "docker"  # standard, а не local-trusted


def test_resolver_rejects_unknown_tenant_and_bad_token(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    resolver = JwtResolver(hub, _SECRET)
    assert resolver.authenticate(f"Bearer {mint_tenant_jwt('ghost', _SECRET)}") is None
    assert resolver.authenticate("Bearer garbage") is None
    assert resolver.authenticate(None) is None


def test_rest_jwt_auth(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    client = TestClient(create_app(resolver=JwtResolver(hub, _SECRET)))
    token = mint_tenant_jwt("alice", _SECRET)
    assert client.get("/runs").status_code == 401
    assert client.get("/runs", headers={"Authorization": "Bearer bad"}).status_code == 401
    assert client.get("/runs", headers={"Authorization": f"Bearer {token}"}).json() == []
