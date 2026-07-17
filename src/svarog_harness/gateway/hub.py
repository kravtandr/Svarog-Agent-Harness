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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from svarog_harness.config.paths import resolve_tenant_config, tenant_home
from svarog_harness.config.schema import SvarogConfig
from svarog_harness.gateway.service import GatewayService
from svarog_harness.tenant import TenantRegistry
from svarog_harness.tenant.models import TenantContext
from svarog_harness.tenant.quota import check_quota, effective_quota

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

    async def shutdown(self) -> None:
        """Graceful shutdown: закрыть тёплые sandbox'ы сессий (ADR-0017)."""


@dataclass
class SingleTenantResolver:
    """Легаси-режим: один сервис + общий bearer-token (или без auth при None)."""

    service: GatewayService
    bearer_token: str | None = None

    async def shutdown(self) -> None:
        await self.service.close_warm_sessions()

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
            tenant_id = ctx.tenant_id
            svc = GatewayService(
                resolved.cfg,
                resolved.workspace,
                on_run_created=lambda run_id: self.registry.record_run(run_id, tenant_id),
                role=ctx.role,
                tenant_id=tenant_id,  # /whoami (ADR-0017 §2)
            )
            svc.quota_guard = self._quota_guard_for(tenant_id, svc)
            self._services[ctx.tenant_id] = svc
        return svc

    def _quota_guard_for(
        self, tenant_id: str, svc: GatewayService
    ) -> Callable[[], Awaitable[None]]:
        async def guard() -> None:
            quota = effective_quota(
                self.base_cfg.tenancy.default_quota, self.registry.get(tenant_id)
            )
            check_quota(await svc.usage(), quota)  # QuotaExceeded

        return guard

    def _service_by_id(self, tenant_id: str) -> GatewayService | None:
        rec = self.registry.get(tenant_id)
        if rec is None:
            return None
        return self.service_for(TenantContext(tenant_id, rec.role))

    async def resume_run(self, run_id: str) -> bool:
        """Возобновить run в его тенанте по run_index (ADR-0014). False — владелец неизвестен."""
        tenant_id = self.registry.tenant_of_run(run_id)
        if tenant_id is None:
            return False
        svc = self._service_by_id(tenant_id)
        if svc is None:
            return False
        await svc.resume_run(run_id)
        return True

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

    async def shutdown(self) -> None:
        """Закрыть тёплые sandbox'ы всех материализованных тенантов (ADR-0017)."""
        for svc in self._services.values():
            await svc.close_warm_sessions()

    async def run_supervisor(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        """Per-tenant refuel-супервизор по run_index (ADR-0014 #5).

        Каждый интервал берёт тенантов, у которых есть зарегистрированные run'ы
        (`run_index`), материализует их сервис и делает `supervise_once`. Так
        refuel-suspended run поднимается, даже если тенант ещё не «оживал»
        входящим запросом. Ошибка одного тенанта не рвёт цикл.
        """
        interval = self.base_cfg.supervisor.interval_sec
        while should_stop is None or not should_stop():
            for tenant_id in self.registry.active_tenant_ids():
                svc = self._service_by_id(tenant_id)
                if svc is None:
                    continue
                with contextlib.suppress(Exception):
                    await svc.supervise_once()
            await asyncio.sleep(interval)
