"""FastAPI-приложение gateway (§10.4): REST + WebSocket поверх GatewayService.

Транспортный слой — без логики агента (§6.1): парсит запрос, зовёт
GatewayService, сериализует ответ. Approval асинхронный: POST решения
фиксирует его и запускает возобновление run'а в фоне (ADR-0005).
"""

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from svarog_harness.gateway.hub import GatewayResolver, SingleTenantResolver, TenantHub
from svarog_harness.gateway.models import (
    AnswerRequest,
    ApprovalDecisionRequest,
    ApprovalView,
    CancelView,
    CreateRunRequest,
    CreateSessionRequest,
    CreateWorkspaceRequest,
    DirListing,
    FileEntry,
    RunDetail,
    RunDiffView,
    RunRef,
    RunSummary,
    SendMessageRequest,
    SessionView,
    SkillCard,
    WhoamiView,
    WorkspaceView,
)
from svarog_harness.gateway.service import CancelNotAllowedError, GatewayService
from svarog_harness.gitflow.provision import (
    CloneError,
    RepoUrlError,
    UnknownWorkspaceError,
    WorkspaceExistsError,
    WorkspaceLimitError,
    WorkspaceNameError,
)
from svarog_harness.tenant.quota import QuotaExceededError
from svarog_harness.trace.lookup import (
    ApprovalNotFoundError,
    RunNotFoundError,
    SessionNotFoundError,
)
from svarog_harness.trace.recorder import WorkspaceBusyError


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
            run_id = await service.create_run(
                req.task, req.autonomy, repo=req.repo, workspace_name=req.workspace
            )
        except QuotaExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from None
        except UnknownWorkspaceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except WorkspaceBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except (RepoUrlError, WorkspaceNameError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except CloneError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
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

    @app.post("/runs/{run_id}/resume", response_model=RunRef)
    async def resume_run(run_id: str, service: ServiceDep) -> RunRef:
        # Явное возобновление suspended-run (ADR-0017 §2): проверяем, что run
        # существует, до фонового resume — иначе 404 некому вернуть.
        try:
            await service.get_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        await service.resume_run(run_id)
        return RunRef(run_id=run_id, state="running")

    @app.get("/runs/{run_id}/diff", response_model=RunDiffView)
    async def run_diff(run_id: str, service: ServiceDep) -> RunDiffView:
        try:
            return await service.run_diff(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.post("/runs/{run_id}/cancel", response_model=CancelView)
    async def cancel_run(run_id: str, service: ServiceDep) -> CancelView:
        try:
            return await service.cancel_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except CancelNotAllowedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.get("/runs/{run_id}/events/stream")
    async def run_events_stream(run_id: str, service: ServiceDep) -> StreamingResponse:
        """NDJSON-стрим событий run'а: HTTP-аналог WS для thin CLI (ADR-0017 §3).

        Клиенту достаточно httpx: строка = JSON-событие, стрим закрывается
        после run_finished.
        """

        async def lines() -> AsyncIterator[bytes]:
            async for event in service.stream(run_id):
                yield (json.dumps(event, ensure_ascii=False) + "\n").encode()

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @app.get("/whoami", response_model=WhoamiView)
    async def whoami(service: ServiceDep) -> WhoamiView:
        return await service.whoami()

    # --- сессии gateway-chat (ADR-0017 §2) --------------------------------

    @app.post("/sessions", response_model=SessionView, status_code=201)
    async def create_session(req: CreateSessionRequest, service: ServiceDep) -> SessionView:
        try:
            return await service.create_session(
                title=req.title, repo=req.repo, workspace_name=req.workspace
            )
        except UnknownWorkspaceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except WorkspaceBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except (RepoUrlError, WorkspaceNameError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except CloneError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @app.get("/sessions/{session_id}", response_model=SessionView)
    async def get_session(session_id: str, service: ServiceDep) -> SessionView:
        try:
            return await service.get_session(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.post("/sessions/{session_id}/messages", response_model=RunRef, status_code=201)
    async def send_message(session_id: str, req: SendMessageRequest, service: ServiceDep) -> RunRef:
        try:
            run_id = await service.send_message(session_id, req.text, req.autonomy)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except UnknownWorkspaceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except WorkspaceBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except QuotaExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from None
        return RunRef(run_id=run_id, state="running")

    # --- named workspaces (ADR-0017 §1/§2) --------------------------------

    @app.post("/workspaces", response_model=WorkspaceView, status_code=201)
    async def create_workspace(req: CreateWorkspaceRequest, service: ServiceDep) -> WorkspaceView:
        try:
            return await service.create_workspace(req.name)
        except WorkspaceExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except WorkspaceLimitError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from None
        except WorkspaceNameError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/workspaces", response_model=list[WorkspaceView])
    async def list_workspaces(service: ServiceDep) -> list[WorkspaceView]:
        return await service.list_workspaces()

    @app.delete("/workspaces/{name}", status_code=204)
    async def delete_workspace(name: str, service: ServiceDep) -> None:
        try:
            await service.delete_workspace(name)
        except (UnknownWorkspaceError, WorkspaceNameError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except WorkspaceBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.get("/workspaces/{name}/files", response_model=None)
    async def workspace_files(
        name: str, service: ServiceDep, path: str = "."
    ) -> FileResponse | JSONResponse:
        """Листинг каталога (JSON) или скачивание файла named workspace."""
        try:
            target = service.workspace_target(name, path)
        except UnknownWorkspaceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except WorkspaceNameError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if target.is_dir():
            entries = [
                FileEntry(
                    name=child.name,
                    is_dir=child.is_dir(),
                    size_bytes=child.stat().st_size if child.is_file() else 0,
                )
                for child in sorted(target.iterdir())
            ]
            listing = DirListing(path=path, entries=entries)
            return JSONResponse(listing.model_dump())
        if target.is_file():
            return FileResponse(target, filename=target.name)
        raise HTTPException(status_code=404, detail=f"нет такого пути в workspace: {path}")

    @app.get("/workspaces/{name}/archive")
    async def workspace_archive(name: str, service: ServiceDep) -> FileResponse:
        """tar.gz снапшот workspace (транспорт результатов не-git workspace'а)."""
        try:
            archive = service.archive_workspace(name)
        except UnknownWorkspaceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except WorkspaceNameError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return FileResponse(
            archive,
            filename=f"{name}.tar.gz",
            media_type="application/gzip",
            background=BackgroundTask(os.unlink, archive),
        )

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
    async def answer_question(approval_id: str, req: AnswerRequest, service: ServiceDep) -> RunRef:
        try:
            run_id = await service.answer_question(approval_id, answer=req.answer)
        except ApprovalNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        # Ответ на ask_user записан — возобновляем run (§6.5, ADR-0005).
        await service.resume_run(run_id)
        return RunRef(run_id=run_id, state="running")

    return app
