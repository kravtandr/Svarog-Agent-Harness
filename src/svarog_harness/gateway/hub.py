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
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from svarog_harness.config.loader import load_config
from svarog_harness.config.paths import resolve_tenant_config, tenant_home
from svarog_harness.config.schema import SvarogConfig
from svarog_harness.gateway.models import FsEntryView, FsListingView, RecentRootView, SessionSummary
from svarog_harness.gateway.roots import WorkspaceRootsRegistry
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


class RootPathError(ValueError):
    """Кандидат в корень не существует или не каталог (422)."""


class RootGoneError(LookupError):
    """Корень сессии/run'а удалён с диска (410 Gone)."""


@dataclass
class WorkspaceHub:
    """Мультиплекс GatewayService по папкам-корням (спека 2026-07-30).

    Как TenantHub, но ключ — путь: каждый корень получает сервис со своим
    конфигом (`load_config(project_dir=root)`), памятью и скиллами. Auth —
    общий bearer, как в SingleTenantResolver: фича живёт только в
    single-tenant, в multi-tenant режиме хаб не создаётся вовсе.
    """

    base_cfg: SvarogConfig
    default_root: Path
    registry: WorkspaceRootsRegistry
    bearer_token: str | None = None
    _services: dict[Path, GatewayService] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.default_root = self.default_root.expanduser().resolve()
        # Сервис корня запуска — из уже загруженного конфига, без второго load.
        self._services[self.default_root] = self._make_service(self.base_cfg, self.default_root)

    def _make_service(self, cfg: SvarogConfig, root: Path) -> GatewayService:
        # Колбэки пишут карты маршрутизации; они же обновляют last_used корня.
        return GatewayService(
            cfg,
            root,
            on_run_created=lambda run_id: self.registry.record_run(run_id, root),
            on_session_created=lambda session_id: self.registry.record_session(session_id, root),
        )

    def service_for(self, path: str | Path) -> GatewayService:
        """Сервис произвольного корня; несуществующий/не-каталог — RootPathError."""
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise RootPathError(f"не каталог или не существует: {root}")
        svc = self._services.get(root)
        if svc is None:
            svc = self._make_service(load_config(project_dir=root), root)
            self._services[root] = svc
        return svc

    def route(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        root: str | None = None,
    ) -> GatewayService:
        """Сервис запроса: заголовок X-Svarog-Root → id → default_root.

        Промах реестра — default_root: сессии, созданные до фичи, работают
        без миграции. Известный, но исчезнувший корень — RootGoneError (410).
        """
        if root is not None:
            return self.service_for(root)
        target: Path | None = None
        if session_id is not None:
            target = self.registry.root_of_session(session_id)
        elif run_id is not None:
            target = self.registry.root_of_run(run_id)
        if target is None:
            return self._services[self.default_root]
        if not target.is_dir():
            raise RootGoneError(f"каталог сессии удалён: {target}")
        return self.service_for(target)

    def authenticate(
        self, authorization: str | None, *, query_token: str | None = None
    ) -> GatewayService | None:
        """Auth-гейт как у SingleTenantResolver; выбор сервиса — в route()."""
        if self.bearer_token is None:
            return self._services[self.default_root]
        token = extract_bearer(authorization) or query_token
        return self._services[self.default_root] if token == self.bearer_token else None

    @property
    def supervisor_enabled(self) -> bool:
        return self.base_cfg.supervisor.auto_resume_refuel

    async def run_supervisor(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        """Refuel-супервизор по корням с записанными run'ами (как TenantHub)."""
        interval = self.base_cfg.supervisor.interval_sec
        while should_stop is None or not should_stop():
            for root in {self.default_root, *self.registry.roots_with_runs()}:
                if not root.is_dir():
                    continue  # исчезнувший корень: run поднимется, когда папка вернётся
                with contextlib.suppress(Exception):
                    await self.service_for(root).supervise_once()
            await asyncio.sleep(interval)

    async def shutdown(self) -> None:
        """Закрыть тёплые sandbox'ы всех материализованных корней (ADR-0017)."""
        for svc in self._services.values():
            await svc.close_warm_sessions()

    async def list_sessions(self, limit: int = 50) -> list[SessionSummary]:
        """Агрегированный список: веер по корням реестра + default, дедуп по id.

        Корни без своего db_path делят пользовательскую БД — одна сессия
        приходит из нескольких сервисов; ряды идентичны (meta в строке БД),
        так что первый занявший id выигрывает. Исчезнувший корень пропускаем:
        его сессии вернутся вместе с папкой (реестр — кэш, не истина).
        """
        seen: dict[str, SessionSummary] = {}
        candidates = [self.default_root] + [root for root, _ in self.registry.roots()]
        for root in candidates:
            try:
                svc = self.service_for(root)
            except RootPathError:
                continue
            for summary in await svc.list_sessions(limit=limit):
                seen.setdefault(summary.session_id, summary)
        ordered = sorted(seen.values(), key=lambda s: s.updated_at, reverse=True)
        return ordered[:limit]

    def list_fs(self, path: str | None) -> FsListingView:
        """Подкаталоги для пикера: только каталоги, скрытые отфильтрованы."""
        base = Path(path).expanduser() if path else Path.home()
        try:
            base = base.resolve(strict=True)
        except OSError as exc:
            raise RootPathError(f"нет такого каталога: {path}") from exc
        if not base.is_dir():
            raise RootPathError(f"не каталог: {base}")
        try:
            children = sorted(base.iterdir(), key=lambda p: p.name.lower())
        except PermissionError as exc:
            raise RootPathError(f"нет доступа: {base}") from exc
        entries: list[FsEntryView] = []
        for child in children:
            try:
                if child.name.startswith(".") or not child.is_dir():
                    continue
                accessible = os.access(child, os.R_OK | os.X_OK)
            except OSError:
                continue  # битый symlink и подобное — просто пропускаем
            entries.append(FsEntryView(name=child.name, path=str(child), accessible=accessible))
        parent = None if base == base.parent else str(base.parent)
        return FsListingView(path=str(base), parent=parent, entries=entries)

    def recent_roots(self) -> list[RecentRootView]:
        """Недавние корни для пикера; несуществующие помечены, не выброшены."""
        return [
            # registry.roots() отдаёт ISO-строку (сырое поле JSON) — парсим
            # в datetime здесь, а не меняем контракт реестра ради одного поля.
            RecentRootView(
                path=str(root), exists=root.is_dir(), last_used=datetime.fromisoformat(ts)
            )
            for root, ts in self.registry.roots()
        ]
