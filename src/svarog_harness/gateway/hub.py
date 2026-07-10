"""TenantHub: мультиплекс GatewayService по тенантам + per-tenant auth (ADR-0014).

Хаб держит один `GatewayService` на тенанта (ленивое создание из резолвнутого
per-tenant cfg через `config.paths.resolve_tenant_config`) и резолвит
per-tenant bearer-token в тенанта через `TenantRegistry` (principal
`gateway:<token>`). Auth и выбор сервиса объединены в один резолвер, который
`create_app` дёргает на каждый запрос: одно приложение обслуживает всех
тенантов, но каждый run исполняется в изоляции своего agent-home.

`SingleTenantResolver` сохраняет прежнее поведение (`tenancy.enabled=false`):
один сервис, один общий bearer-token или полностью открытый режим без токена.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from svarog_harness.config.paths import resolve_tenant_config, tenant_home
from svarog_harness.config.schema import SvarogConfig
from svarog_harness.gateway.service import GatewayService
from svarog_harness.tenant import TenantRegistry
from svarog_harness.tenant.models import TenantContext

_BEARER_PREFIX = "Bearer "


def extract_bearer(authorization: str | None) -> str | None:
    """Значение токена из заголовка `Authorization: Bearer <token>`, иначе None."""
    if authorization and authorization.startswith(_BEARER_PREFIX):
        return authorization[len(_BEARER_PREFIX) :]
    return None


class GatewayResolver(Protocol):
    """Единая точка auth + выбора сервиса для `create_app` (single/multi-tenant)."""

    def authenticate(
        self, authorization: str | None, *, query_token: str | None = None
    ) -> GatewayService | None:
        """Сервис аутентифицированного клиента, либо None (401)."""

    @property
    def supervisor_enabled(self) -> bool:
        """Нужен ли refuel-супервизор в lifespan (§6.10)."""

    async def run_supervisor(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        """Периодическое авто-поднятие refuel-suspended runs, пока gateway жив."""


@dataclass
class SingleTenantResolver:
    """Легаси-режим: один сервис + общий bearer-token (или без auth при None)."""

    service: GatewayService
    bearer_token: str | None = None

    def authenticate(
        self, authorization: str | None, *, query_token: str | None = None
    ) -> GatewayService | None:
        if self.bearer_token is None:
            return self.service  # auth не настроен — открытый режим (как раньше)
        token = extract_bearer(authorization) or query_token
        return self.service if token == self.bearer_token else None

    @property
    def supervisor_enabled(self) -> bool:
        return self.service.cfg.supervisor.auto_resume_refuel

    async def run_supervisor(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        await self.service.run_supervisor(should_stop=should_stop)


@dataclass
class TenantHub:
    """Мультиплекс GatewayService по тенантам с per-tenant bearer-auth (ADR-0014)."""

    base_cfg: SvarogConfig
    registry: TenantRegistry
    _services: dict[str, GatewayService] = field(default_factory=dict, init=False)

    def service_for(self, ctx: TenantContext) -> GatewayService:
        """GatewayService тенанта (ленивое создание из резолвнутого per-tenant cfg)."""
        svc = self._services.get(ctx.tenant_id)
        if svc is None:
            resolved = resolve_tenant_config(
                self.base_cfg,
                tenant_id=ctx.tenant_id,
                home=tenant_home(self.base_cfg, ctx.tenant_id),
                role=ctx.role,
                shared_skills=self.base_cfg.tenancy.shared_skills,
            )
            svc = GatewayService(resolved.cfg, resolved.workspace)
            self._services[ctx.tenant_id] = svc
        return svc

    def resolve(
        self, authorization: str | None, *, query_token: str | None = None
    ) -> tuple[TenantContext, GatewayService] | None:
        """token → (контекст тенанта, его сервис); None — неизвестный/пустой токен."""
        token = extract_bearer(authorization) or query_token
        if not token:
            return None
        ctx = self.registry.resolve_principal(f"gateway:{token}")
        if ctx is None:
            return None
        return ctx, self.service_for(ctx)

    def authenticate(
        self, authorization: str | None, *, query_token: str | None = None
    ) -> GatewayService | None:
        resolved = self.resolve(authorization, query_token=query_token)
        return resolved[1] if resolved is not None else None

    @property
    def supervisor_enabled(self) -> bool:
        return self.base_cfg.supervisor.auto_resume_refuel

    async def run_supervisor(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        """Супервизит ЖИВЫЕ tenant-сервисы каждый интервал.

        Полный scan `run_index` для тенантов без активного сервиса — следующий
        срез Фазы 2 (ADR-0014 #5): пока refuel-suspended run поднимается, как
        только его тенант «оживёт» первым запросом.
        """
        interval = self.base_cfg.supervisor.interval_sec
        while should_stop is None or not should_stop():
            for svc in list(self._services.values()):
                with contextlib.suppress(Exception):
                    await svc.supervise_once()
            await asyncio.sleep(interval)
