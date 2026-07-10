"""FastAPI-приложение gateway (§10.4): REST + WebSocket поверх GatewayService.

Транспортный слой — без логики агента (§6.1): парсит запрос, зовёт
GatewayService, сериализует ответ. Approval асинхронный: POST решения
фиксирует его и запускает возобновление run'а в фоне (ADR-0005).
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status

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
from svarog_harness.trace.lookup import ApprovalNotFoundError, RunNotFoundError


def _authorized(authorization: str | None, token: str | None) -> bool:
    if token is None:
        return True
    return authorization == f"Bearer {token}"


def _auth_dependency(token: str | None) -> Callable[[str | None], None]:
    def check(authorization: Annotated[str | None, Header()] = None) -> None:
        if not _authorized(authorization, token):
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    return check


def create_app(service: GatewayService, *, bearer_token: str | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Супервизор refuel (§6.10): авто-поднятие refuel-suspended runs, пока
        # gateway жив. Запускается только при старте приложения (lifespan), а не
        # при простом создании TestClient без контекст-менеджера.
        task: asyncio.Task[None] | None = None
        if service.cfg.supervisor.auto_resume_refuel:
            task = asyncio.ensure_future(service.run_supervisor())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Svarog Gateway", version="0.1.0", lifespan=lifespan)
    auth = [Depends(_auth_dependency(bearer_token))] if bearer_token is not None else []

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "workspace": str(service.workspace)}

    @app.post("/runs", response_model=RunRef, status_code=201, dependencies=auth)
    async def create_run(req: CreateRunRequest) -> RunRef:
        run_id = await service.create_run(req.task, req.autonomy)
        return RunRef(run_id=run_id, state="running")

    @app.get("/runs", response_model=list[RunSummary], dependencies=auth)
    async def list_runs(limit: int = 20) -> list[RunSummary]:
        return await service.list_runs(limit=limit)

    @app.get("/runs/{run_id}", response_model=RunDetail, dependencies=auth)
    async def get_run(run_id: str) -> RunDetail:
        try:
            return await service.get_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.websocket("/runs/{run_id}/events")
    async def run_events(websocket: WebSocket, run_id: str) -> None:
        query_token = websocket.query_params.get("token")
        authorization = websocket.headers.get("authorization")
        if bearer_token is not None and not (
            _authorized(authorization, bearer_token) or query_token == bearer_token
        ):
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

    @app.get("/skills", response_model=list[SkillCard], dependencies=auth)
    async def list_skills() -> list[SkillCard]:
        return service.list_skills()

    @app.get("/approvals", response_model=list[ApprovalView], dependencies=auth)
    async def list_approvals() -> list[ApprovalView]:
        return await service.list_pending_approvals()

    @app.post("/approvals/{approval_id}", response_model=RunRef, dependencies=auth)
    async def decide_approval(approval_id: str, req: ApprovalDecisionRequest) -> RunRef:
        try:
            run_id = await service.decide_approval(
                approval_id, approved=req.approved, reason=req.reason
            )
        except ApprovalNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        # Решение принято — возобновляем run в фоне (ADR-0005: approval асинхронный).
        await service.resume_run(run_id)
        return RunRef(run_id=run_id, state="running")

    @app.post("/approvals/{approval_id}/answer", response_model=RunRef, dependencies=auth)
    async def answer_question(approval_id: str, req: AnswerRequest) -> RunRef:
        try:
            run_id = await service.answer_question(approval_id, answer=req.answer)
        except ApprovalNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        # Ответ на ask_user записан — возобновляем run (§6.5, ADR-0005).
        await service.resume_run(run_id)
        return RunRef(run_id=run_id, state="running")

    return app
