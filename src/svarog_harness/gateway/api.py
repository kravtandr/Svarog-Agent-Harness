"""FastAPI-приложение gateway (§10.4): REST + WebSocket поверх GatewayService.

Транспортный слой — без логики агента (§6.1): парсит запрос, зовёт
GatewayService, сериализует ответ. Approval асинхронный: POST решения
фиксирует его и запускает возобновление run'а в фоне (ADR-0005).
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status

from svarog_harness.gateway.hub import GatewayResolver, SingleTenantResolver, TenantHub
from svarog_harness.gateway.models import (
    AnswerRequest,
    ApprovalDecisionRequest,
    ApprovalView,
    CreateRunRequest,
    RunDetail,
    RunRef,
    RunSummary,
    SkillCard,
)
from svarog_harness.gateway.service import GatewayService
from svarog_harness.tenant.quota import QuotaExceededError
from svarog_harness.trace.lookup import ApprovalNotFoundError, RunNotFoundError


def create_app(
    service: GatewayService | None = None,
    *,
    bearer_token: str | None = None,
    hub: TenantHub | None = None,
    resolver: GatewayResolver | None = None,
) -> FastAPI:
    """REST/WS-приложение над сервисом (single-tenant), хабом или резолвером.

    Auth и выбор сервиса объединены в резолвер: single-tenant — общий bearer
    (или открытый режим без токена), multi-tenant — per-tenant token → тенант
    через реестр, либо явный `resolver` (напр. JWT, ADR-0014 Фаза 3). Каждый
    защищённый роут получает сервис аутентифицированного тенанта через
    зависимость `_require_service`.
    """
    if resolver is None:
        if hub is not None:
            resolver = hub
        elif service is not None:
            resolver = SingleTenantResolver(service, bearer_token)
        else:
            raise ValueError("create_app: нужен service, hub или resolver")

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Супервизор refuel (§6.10): авто-поднятие refuel-suspended runs, пока
        # gateway жив. Запускается только при старте приложения (lifespan), а не
        # при простом создании TestClient без контекст-менеджера.
        task: asyncio.Task[None] | None = None
        if resolver.supervisor_enabled:
            task = asyncio.ensure_future(resolver.run_supervisor())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Svarog Gateway", version="0.1.0", lifespan=lifespan)

    def _require_service(
        authorization: Annotated[str | None, Header()] = None,
    ) -> GatewayService:
        svc = resolver.authenticate(authorization)
        if svc is None:
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")
        return svc

    ServiceDep = Annotated[GatewayService, Depends(_require_service)]  # noqa: N806 — тип-алиас

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs", response_model=RunRef, status_code=201)
    async def create_run(req: CreateRunRequest, service: ServiceDep) -> RunRef:
        try:
            run_id = await service.create_run(req.task, req.autonomy)
        except QuotaExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from None
        return RunRef(run_id=run_id, state="running")

    @app.get("/runs", response_model=list[RunSummary])
    async def list_runs(service: ServiceDep, limit: int = 20) -> list[RunSummary]:
        return await service.list_runs(limit=limit)

    @app.get("/runs/{run_id}", response_model=RunDetail)
    async def get_run(run_id: str, service: ServiceDep) -> RunDetail:
        try:
            return await service.get_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.websocket("/runs/{run_id}/events")
    async def run_events(websocket: WebSocket, run_id: str) -> None:
        query_token = websocket.query_params.get("token")
        authorization = websocket.headers.get("authorization")
        service = resolver.authenticate(authorization, query_token=query_token)
        if service is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await websocket.accept()
        try:
            async for event in service.stream(run_id):
                await websocket.send_json(event)
        except WebSocketDisconnect:
            return
        # Стрим завершился (run_finished в истории/живой) — закрываем соединение.
        await websocket.close()

    @app.get("/skills", response_model=list[SkillCard])
    async def list_skills(service: ServiceDep) -> list[SkillCard]:
        return service.list_skills()

    @app.get("/approvals", response_model=list[ApprovalView])
    async def list_approvals(service: ServiceDep) -> list[ApprovalView]:
        return await service.list_pending_approvals()

    @app.post("/approvals/{approval_id}", response_model=RunRef)
    async def decide_approval(
        approval_id: str, req: ApprovalDecisionRequest, service: ServiceDep
    ) -> RunRef:
        try:
            run_id = await service.decide_approval(
                approval_id, approved=req.approved, reason=req.reason
            )
        except ApprovalNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        # Решение принято — возобновляем run в фоне (ADR-0005: approval асинхронный).
        await service.resume_run(run_id)
        return RunRef(run_id=run_id, state="running")

    @app.post("/approvals/{approval_id}/answer", response_model=RunRef)
    async def answer_question(
        approval_id: str, req: AnswerRequest, service: ServiceDep
    ) -> RunRef:
        try:
            run_id = await service.answer_question(approval_id, answer=req.answer)
        except ApprovalNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        # Ответ на ask_user записан — возобновляем run (§6.5, ADR-0005).
        await service.resume_run(run_id)
        return RunRef(run_id=run_id, state="running")

    return app
