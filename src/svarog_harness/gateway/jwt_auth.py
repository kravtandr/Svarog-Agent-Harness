"""JWT-бэкенд auth для gateway (ADR-0014, Фаза 3) за интерфейсом GatewayResolver.

Альтернатива статичным per-tenant bearer-токенам: клиент предъявляет
подписанный JWT (HS256), `sub` — tenant_id. Токен подтверждает личность
stateless (подпись нашим ключом), но **роль берётся из реестра** — источника
истины (ADR-0013), а не из claim'а, поэтому утечка/подмена claim'а роль не
поднимает. Тенант обязан существовать в реестре.

HS256 реализован на stdlib (hmac/hashlib/base64) — без внешней зависимости;
сравнение подписи — constant-time. Достаточно для gateway; RS256/JWKS — при
необходимости за тем же интерфейсом.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from svarog_harness.gateway.hub import TenantHub, extract_bearer
from svarog_harness.gateway.service import GatewayService
from svarog_harness.tenant.models import TenantContext


class InvalidTokenError(Exception):
    """JWT не прошёл проверку подписи/срока/структуры."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def encode_hs256(claims: dict[str, Any], secret: str) -> str:
    """Подписать claims как компактный JWT (HS256)."""
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url_encode(sig)}"


def decode_hs256(token: str, secret: str) -> dict[str, Any]:
    """Проверить подпись и `exp`, вернуть payload. Иначе — InvalidTokenError.

    `binascii.Error` и `json.JSONDecodeError` — подклассы `ValueError`, поэтому
    любые ошибки декодирования base64/JSON ловятся одним `except ValueError`.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise InvalidTokenError("некорректная структура JWT")
    header_b64, payload_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    try:
        given = _b64url_decode(sig_b64)
        if not hmac.compare_digest(given, expected):
            raise InvalidTokenError("подпись не совпадает")
        header = json.loads(_b64url_decode(header_b64))
        payload: dict[str, Any] = json.loads(_b64url_decode(payload_b64))
    except ValueError as exc:
        raise InvalidTokenError("JWT не декодируется") from exc
    if header.get("alg") != "HS256":
        raise InvalidTokenError(f"неподдерживаемый alg: {header.get('alg')}")
    exp = payload.get("exp")
    if exp is not None and time.time() > float(exp):
        raise InvalidTokenError("токен истёк")
    return payload


def mint_tenant_jwt(tenant_id: str, secret: str, *, ttl_sec: int = 3600) -> str:
    """Выпустить JWT для тенанта: sub=tenant_id, exp через ttl_sec."""
    now = int(time.time())
    return encode_hs256({"sub": tenant_id, "iat": now, "exp": now + ttl_sec}, secret)


@dataclass
class JwtResolver:
    """GatewayResolver на JWT: token → sub → тенант из реестра (роль из реестра)."""

    hub: TenantHub
    secret: str

    def authenticate(
        self, authorization: str | None, *, query_token: str | None = None
    ) -> GatewayService | None:
        token = extract_bearer(authorization) or query_token
        if not token:
            return None
        try:
            claims = decode_hs256(token, self.secret)
        except InvalidTokenError:
            return None
        tenant_id = claims.get("sub")
        if not isinstance(tenant_id, str):
            return None
        record = self.hub.registry.get(tenant_id)  # источник истины по роли (ADR-0013)
        if record is None:
            return None
        return self.hub.service_for(TenantContext(tenant_id, record.role))

    @property
    def supervisor_enabled(self) -> bool:
        return self.hub.supervisor_enabled

    async def run_supervisor(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        await self.hub.run_supervisor(should_stop=should_stop)
