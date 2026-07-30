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

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from svarog_harness.config.loader import ConfigError
from svarog_harness.config.paths import WorkspaceLayoutError
from svarog_harness.gateway.attachments import (
    MAX_UPLOAD_BYTES,
    AttachmentPathError,
    AttachmentTooLarge,
    AttachmentTypeError,
)
from svarog_harness.gateway.catalog import CatalogError
from svarog_harness.gateway.commands import WEB_COMMANDS
from svarog_harness.gateway.hub import (
    GatewayResolver,
    RootGoneError,
    RootPathError,
    SingleTenantResolver,
    TenantHub,
    WorkspaceHub,
)
from svarog_harness.gateway.models import (
    AnswerRequest,
    ApprovalDecisionRequest,
    ApprovalView,
    AttachmentView,
    CancelView,
    CreateRunRequest,
    CreateSessionRequest,
    CreateWorkspaceRequest,
    DirListing,
    ExecutorOptionView,
    FileEntry,
    FileSuggestionView,
    FsListingView,
    MemoryFileView,
    MemoryHitView,
    MemoryPageView,
    ModelCardView,
    ProviderView,
    RecentRootView,
    RunDetail,
    RunDiffView,
    RunRef,
    RunSummary,
    SecretView,
    SendMessageRequest,
    SessionSummary,
    SessionThread,
    SessionView,
    SkillCard,
    SlashCommandView,
    WhoamiView,
    WorkspaceView,
)
from svarog_harness.gateway.overrides import OverrideError, RunOverride
from svarog_harness.gateway.service import (
    CancelNotAllowedError,
    GatewayService,
    MemoryDisabledError,
    MemoryPathError,
    SessionBusyError,
    UnknownProviderError,
)
from svarog_harness.gateway.settings import ConfigDiffView, ConfigUpdateRequest, ConfigView
from svarog_harness.gateway.static import web_dist_dir
from svarog_harness.gitflow.provision import (
    CloneError,
    RepoUrlError,
    UnknownWorkspaceError,
    WorkspaceExistsError,
    WorkspaceLimitError,
    WorkspaceNameError,
)
from svarog_harness.llm.openai_compatible import ApiKeyError
from svarog_harness.sandbox.base import SandboxError
from svarog_harness.tenant.quota import QuotaExceededError
from svarog_harness.tools.document_tools import _IMAGE_MIME
from svarog_harness.trace.lookup import (
    ApprovalNotFoundError,
    RunNotFoundError,
    SessionNotFoundError,
)
from svarog_harness.trace.recorder import WorkspaceBusyError


async def _read_capped(file: UploadFile, limit: int) -> bytes:
    """Прочитать тело с потолком: за лимитом обрываем, не дочитывая до конца.

    Иначе `MAX_UPLOAD_BYTES` проверялся бы уже после того, как всё тело
    оказалось в памяти, — то есть не защищал бы ровно от того, ради чего
    заведён (ADR-0014: тенантов в процессе может быть несколько).
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise AttachmentTooLarge(f"файл больше потолка {limit} байт")
        chunks.append(chunk)
    return b"".join(chunks)


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
            # Тёплые sandbox'ы сессий не переживают процесс (ADR-0017);
            # осиротевшие при аварии подметает GC по PID владельца (ADR-0016).
            with contextlib.suppress(Exception):
                await resolver.shutdown()

    app = FastAPI(title="Svarog Gateway", version="0.1.0", lifespan=lifespan)

    # CORS нужен только режиму раздельной разработки: в бою статика едет
    # с того же origin, что и API, и заголовки не требуются.
    #
    # Имя переменной намеренно без префикса SVAROG_: pydantic-settings
    # разбирает SVAROG_GATEWAY__* как поле секции gateway, а GatewayConfig —
    # StrictModel, поэтому такая переменная роняла загрузку конфига целиком.
    origins = [o for o in os.environ.get("GORN_CORS_ORIGINS", "").split(",") if o]
    if "*" in origins:
        # Звёздочка вместе с allow_credentials — заряженный footgun; лучше
        # отказать явно, чем открыть API всему миру по опечатке.
        raise ValueError("GORN_CORS_ORIGINS='*' не поддерживается: перечислите origin'ы явно")
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def _require_service(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_svarog_root: Annotated[str | None, Header()] = None,
    ) -> GatewayService:
        svc = resolver.authenticate(authorization)
        if svc is None:
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")
        if isinstance(resolver, WorkspaceHub):
            # Маршрутизация по корню: id из пути URL (какой найдётся) либо
            # явный заголовок; сессии до фичи проваливаются в default_root.
            try:
                return resolver.route(
                    session_id=request.path_params.get("session_id"),
                    run_id=request.path_params.get("run_id"),
                    root=x_svarog_root,
                )
            except RootPathError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
            except RootGoneError as exc:
                if request.method == "DELETE":
                    # Удаление истории должно работать и без папки: строка
                    # сессии живёт в общей БД default-корня независимо от
                    # того, существует ли её каталог, — иначе зомби-сессию
                    # с удалённым корнем нельзя убрать из списка никогда.
                    return resolver.route()
                raise HTTPException(status_code=410, detail=str(exc)) from None
        return svc

    ServiceDep = Annotated[GatewayService, Depends(_require_service)]  # noqa: N806 — тип-алиас

    def _service_for_path(path: str | None, fallback: GatewayService) -> GatewayService:
        """Сервис корня из `path` тела create-запроса (спека 2026-07-30)."""
        if path is None:
            return fallback
        if not isinstance(resolver, WorkspaceHub):
            raise HTTPException(
                status_code=422, detail="path поддерживается только в single-tenant режиме"
            )
        try:
            return resolver.service_for(path)
        except RootPathError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/commands", response_model=list[SlashCommandView])
    async def list_commands() -> list[SlashCommandView]:
        return [SlashCommandView(**vars(cmd)) for cmd in WEB_COMMANDS]

    @app.post("/runs", response_model=RunRef, status_code=201)
    async def create_run(req: CreateRunRequest, service: ServiceDep) -> RunRef:
        service = _service_for_path(req.path, service)
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

    # --- исполнители composer'а (задача 3) ---------------------------------

    @app.get("/executors", response_model=list[ExecutorOptionView])
    async def list_executors(service: ServiceDep) -> list[ExecutorOptionView]:
        return [ExecutorOptionView(**vars(option)) for option in service.executor_options()]

    # --- каталог моделей (задача 6) ---------------------------------------

    @app.get("/models", response_model=list[ProviderView])
    async def list_providers(service: ServiceDep) -> list[ProviderView]:
        return service.list_providers()

    @app.get("/models/{provider}", response_model=list[ModelCardView])
    async def provider_models(provider: str, service: ServiceDep) -> list[ModelCardView]:
        try:
            cards = await service.provider_models(provider)
        except UnknownProviderError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except (CatalogError, ApiKeyError) as exc:
            # Провайдер недоступен или ключ не найден — это не сбой шлюза:
            # 502 с причиной, чтобы человек увидел, что чинить.
            raise HTTPException(status_code=502, detail=str(exc)) from None
        return [ModelCardView(**vars(card)) for card in cards]

    # --- сессии gateway-chat (ADR-0017 §2) --------------------------------

    @app.post("/sessions", response_model=SessionView, status_code=201)
    async def create_session(req: CreateSessionRequest, service: ServiceDep) -> SessionView:
        service = _service_for_path(req.path, service)
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

    # Раньше маршрута с параметром: иначе "/sessions" уедет в {session_id}.
    @app.get("/sessions", response_model=list[SessionSummary])
    async def list_sessions(service: ServiceDep, limit: int = 50) -> list[SessionSummary]:
        if isinstance(resolver, WorkspaceHub):
            return await resolver.list_sessions(limit=limit)
        return await service.list_sessions(limit=limit)

    @app.get("/sessions/{session_id}", response_model=SessionView)
    async def get_session(session_id: str, service: ServiceDep) -> SessionView:
        try:
            return await service.get_session(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str, service: ServiceDep) -> None:
        try:
            await service.delete_session(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.get("/sessions/{session_id}/messages", response_model=SessionThread)
    async def session_messages(session_id: str, service: ServiceDep) -> SessionThread:
        try:
            return await service.session_thread(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.get("/sessions/{session_id}/files", response_model=list[FileSuggestionView])
    async def session_files(
        session_id: str, service: ServiceDep, q: str = ""
    ) -> list[FileSuggestionView]:
        try:
            found = await service.file_suggestions(session_id, q)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return [FileSuggestionView(path=s.value.removeprefix("@"), label=s.label) for s in found]

    @app.post(
        "/sessions/{session_id}/attachments",
        response_model=AttachmentView,
        status_code=201,
    )
    async def upload_attachment(
        session_id: str, service: ServiceDep, file: Annotated[UploadFile, File()]
    ) -> AttachmentView:
        try:
            # Читаем с тем же потолком, что и store_attachment: иначе тело
            # целиком осядет в памяти ещё до того, как размер кто-то проверит.
            data = await _read_capped(file, MAX_UPLOAD_BYTES)
            stored = await service.store_attachment(session_id, file.filename or "файл", data)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except AttachmentTypeError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from None
        except AttachmentTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from None
        return AttachmentView(**vars(stored))

    @app.get("/sessions/{session_id}/attachments/{name}")
    async def read_attachment(session_id: str, name: str, service: ServiceDep) -> FileResponse:
        try:
            path = await service.attachment_path(session_id, name)
        except (SessionNotFoundError, AttachmentPathError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        mime = _IMAGE_MIME.get(path.suffix.lower())
        if mime is not None:
            # Картинка — единственный сегодняшний потребитель (<img> в
            # ChatScreen); content-type задаём явно из белого списка, не
            # угадываем голым FileResponse.
            return FileResponse(path, media_type=mime)
        # Всё остальное — включая .html из белого списка загрузки — только
        # как скачивание. SPA раздаётся с этого же origin, где в
        # sessionStorage лежит bearer-токен: открытая по прямой ссылке
        # .html-страница не должна получить шанс исполниться в этом origin.
        return FileResponse(path, filename=name)

    @app.post("/sessions/{session_id}/messages", response_model=RunRef, status_code=201)
    async def send_message(session_id: str, req: SendMessageRequest, service: ServiceDep) -> RunRef:
        try:
            run_id = await service.send_message(
                session_id,
                req.text,
                req.autonomy,
                RunOverride(
                    executor=req.executor,
                    provider=req.provider,
                    model=req.model,
                    adapter=req.adapter,
                ),
                attachments=req.attachments,
            )
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except UnknownWorkspaceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except WorkspaceBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except QuotaExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from None
        except (SandboxError, WorkspaceLayoutError) as exc:
            # Автономия, которую исполнитель не умеет (ADR-0016 §6), или
            # workspace, пересекающийся с control-plane (ADR-0015 §0.3).
            # И то и другое — конфигурация запуска, а не сбой сервера:
            # 422 с текстом, а не 500 с трейсбеком в лог.
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except OverrideError as exc:
            # Выбор в поле ввода несовместим с конфигом — это ввод человека,
            # а не сбой сервера: 422 с текстом, который говорит, что делать.
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except AttachmentPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return RunRef(run_id=run_id, state="running")

    # --- обзор ФС для пикера рабочей папки (спека 2026-07-30) -------------
    # Только single-tenant: в multi-tenant режиме маршрутов не существует.
    if isinstance(resolver, WorkspaceHub):
        hub_resolver = resolver

        @app.get("/fs", response_model=FsListingView, dependencies=[Depends(_require_service)])
        async def list_fs(path: str | None = None) -> FsListingView:
            try:
                return hub_resolver.list_fs(path)
            except RootPathError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None

        @app.get(
            "/fs/recent",
            response_model=list[RecentRootView],
            dependencies=[Depends(_require_service)],
        )
        async def recent_roots() -> list[RecentRootView]:
            return hub_resolver.recent_roots()

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
        if service is not None and isinstance(resolver, WorkspaceHub):
            try:
                service = resolver.route(run_id=run_id)
            except (RootPathError, RootGoneError):
                service = None  # закроется ниже как policy violation
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

    # --- память (план 2026-07-27) ------------------------------------------

    def _memory_error(exc: Exception) -> HTTPException:
        # Память не настроена — 404 раздела, а не 500: это конфигурация, не сбой.
        status_code = 404 if isinstance(exc, MemoryDisabledError) else 422
        return HTTPException(status_code=status_code, detail=str(exc))

    @app.get("/memory/tree", response_model=list[MemoryPageView])
    async def memory_tree(service: ServiceDep) -> list[MemoryPageView]:
        try:
            return service.memory_tree()
        except (MemoryDisabledError, MemoryPathError) as exc:
            raise _memory_error(exc) from None

    @app.get("/memory/file", response_model=MemoryFileView)
    async def memory_file(path: str, service: ServiceDep) -> MemoryFileView:
        try:
            return service.memory_file(path)
        except (MemoryDisabledError, MemoryPathError) as exc:
            raise _memory_error(exc) from None

    @app.get("/memory/search", response_model=list[MemoryHitView])
    async def memory_search(q: str, service: ServiceDep, limit: int = 10) -> list[MemoryHitView]:
        try:
            return await service.memory_search(q, limit=limit)
        except (MemoryDisabledError, MemoryPathError) as exc:
            raise _memory_error(exc) from None

    # --- конфигурация и секреты (план 2026-07-27) --------------------------

    @app.get("/config", response_model=ConfigView)
    async def get_config(service: ServiceDep) -> ConfigView:
        return service.describe_config()

    @app.post("/config/preview", response_model=ConfigDiffView)
    async def preview_config(req: ConfigUpdateRequest, service: ServiceDep) -> ConfigDiffView:
        try:
            return service.preview_config(req.values)
        except (ConfigError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.post("/config", response_model=ConfigDiffView)
    async def write_config(req: ConfigUpdateRequest, service: ServiceDep) -> ConfigDiffView:
        try:
            return await service.write_config(req.values)
        except (ConfigError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/secrets", response_model=list[SecretView])
    async def list_secrets(service: ServiceDep) -> list[SecretView]:
        return service.list_secrets()

    # --- собранный клиент (план 2026-07-27) --------------------------------
    # Монтируется последним: маршруты API уже объявлены и в тень не уходят.
    dist = web_dist_dir()
    if dist is not None:
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(dist / "index.html")

    else:

        @app.get("/", include_in_schema=False)
        async def index_missing() -> FileResponse:
            raise HTTPException(
                status_code=404,
                detail=(
                    "Клиент не собран. Соберите его: "
                    "npm --prefix web ci && npm --prefix web run build"
                ),
            )

    return app
