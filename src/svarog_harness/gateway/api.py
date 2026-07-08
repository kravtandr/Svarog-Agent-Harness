"""FastAPI-приложение gateway (§10.4): REST + WebSocket поверх GatewayService.

Транспортный слой — без логики агента (§6.1): парсит запрос, зовёт
GatewayService, сериализует ответ. Approval асинхронный: POST решения
фиксирует его и запускает возобновление run'а в фоне (ADR-0005).
"""

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from svarog_harness.gateway.models import (
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


def create_app(service: GatewayService) -> FastAPI:
    app = FastAPI(title="Svarog Gateway", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "workspace": str(service.workspace)}

    @app.post("/runs", response_model=RunRef, status_code=201)
    async def create_run(req: CreateRunRequest) -> RunRef:
        run_id = await service.create_run(req.task, req.autonomy)
        return RunRef(run_id=run_id, state="running")

    @app.get("/runs", response_model=list[RunSummary])
    async def list_runs(limit: int = 20) -> list[RunSummary]:
        return await service.list_runs(limit=limit)

    @app.get("/runs/{run_id}", response_model=RunDetail)
    async def get_run(run_id: str) -> RunDetail:
        try:
            return await service.get_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.websocket("/runs/{run_id}/events")
    async def run_events(websocket: WebSocket, run_id: str) -> None:
        await websocket.accept()
        try:
            async for event in service.stream(run_id):
                await websocket.send_json(event)
        except WebSocketDisconnect:
            return
        # Стрим завершился (run_finished в истории/живой) — закрываем соединение.
        await websocket.close()

    @app.get("/skills", response_model=list[SkillCard])
    async def list_skills() -> list[SkillCard]:
        return service.list_skills()

    @app.get("/approvals", response_model=list[ApprovalView])
    async def list_approvals() -> list[ApprovalView]:
        return await service.list_pending_approvals()

    @app.post("/approvals/{approval_id}", response_model=RunRef)
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

    return app
