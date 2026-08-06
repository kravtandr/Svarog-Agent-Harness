"""GatewayService: оркестрация runs для внешних интерфейсов (§6.1, §10.4).

Gateway не содержит логики агента — он запускает `TaskRunner` в фоновой
asyncio-задаче, отдаёт клиенту run_id сразу после старта run и стримит
события через `EventStream`. Approval асинхронный (ADR-0005): run уходит в
`waiting_approval`, решение приходит позже любым интерфейсом и возобновляет
run в фоне. Источник истины по trace — SQLite; события — «живой» слой.
"""

import asyncio
import contextlib
import logging
import os
import re
import tarfile
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.cli.chat_completion import Suggestion, at_suggestions
from svarog_harness.config.loader import (
    PROJECT_CONFIG_NAME,
    USER_CONFIG_PATH,
    ConfigError,
    deep_merge,
    load_config,
)
from svarog_harness.config.paths import memory_dir, skills_dirs
from svarog_harness.config.schema import (
    AutonomyMode,
    MCPConfig,
    MCPServerConfig,
    ProviderConfig,
    SvarogConfig,
    TenantRole,
)
from svarog_harness.gateway.attachments import (
    ATTACHMENTS_DIR,
    StoredAttachment,
    attachments_note,
    store_attachment,
    verify_attachment,
)
from svarog_harness.gateway.autotitle import fallback_title, needs_draft, needs_refine, title_for
from svarog_harness.gateway.catalog import CatalogError, ModelCard, fetch_models
from svarog_harness.gateway.executors import (
    ExecutorOption,
    SandboxOption,
    executor_options,
    sandbox_options,
)
from svarog_harness.gateway.models import (
    ApprovalView,
    CancelView,
    McpServerView,
    McpTestView,
    MemoryFileView,
    MemoryHitView,
    MemoryPageView,
    ProviderCheckView,
    ProviderView,
    RepoSpec,
    RunDetail,
    RunDiffView,
    RunSummary,
    SecretView,
    SessionSummary,
    SessionThread,
    SessionView,
    SkillCard,
    ThreadItemView,
    ToolCallView,
    WhoamiView,
    WorkspaceView,
)
from svarog_harness.gateway.overrides import (
    RunOverride,
    apply_override,
    prices_from_meta,
    run_meta_for,
)
from svarog_harness.gateway.session_events import SessionEventHub
from svarog_harness.gateway.settings import (
    ConfigDiffView,
    ConfigView,
    apply_values,
    describe_config,
    diff_lines,
    patch_yaml_text,
    remove_deep_key,
    set_deep_values,
)
from svarog_harness.gitflow.provision import (
    DEFAULT_GIT_CREDENTIALS_REF,
    CloneError,
    UnknownWorkspaceError,
    create_named_workspace,
    delete_named_workspace,
    list_named_workspaces,
    provision_clone,
    resolve_named_workspace,
    resolve_workspace_file,
    sweep_task_workspaces,
    task_workspace_dir,
)
from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.llm.openai_compatible import ApiKeyError, auxiliary_provider, resolve_api_key
from svarog_harness.llm.provider import ChatMessage
from svarog_harness.mcp import MCPBackend, connect_mcp_servers
from svarog_harness.memory.index import search as memory_search
from svarog_harness.runtime.agents import EXTERNAL_ADAPTERS
from svarog_harness.runtime.loop import RunOutcome
from svarog_harness.runtime.orchestrator import RunHooks, SessionResources, TaskRunner
from svarog_harness.runtime.summaries import short_arg, short_result
from svarog_harness.secrets import default_secret_store
from svarog_harness.secrets.store import FileSecretStore
from svarog_harness.skills import scan_skills
from svarog_harness.storage.events import EventStream, InProcessEventStream
from svarog_harness.storage.models import Run, RunState, Session, ToolCall, ToolCallStatus
from svarog_harness.tenant.quota import QuotaUsage
from svarog_harness.trace.lookup import (
    ApprovalNotFoundError,
    RunNotFoundError,
    find_run_by_prefix,
    find_session_by_prefix,
)
from svarog_harness.trace.recorder import TraceRecorder, WorkspaceBusyError
from svarog_harness.trace.viewer import fetch_run, fetch_runs, run_usage_totals
from svarog_harness.verifier import CheckOutcome

logger = logging.getLogger(__name__)

# Диф от корня истории, когда первый коммит run'а — root commit.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
# Retention-GC task-workspace'ов гоняется не чаще раза в час (ADR-0017).
_GC_INTERVAL_SEC = 3600.0
# Незавершённые состояния: их workspace GC не трогает (resume должен работать).
_LIVE_STATES = (RunState.PENDING, RunState.RUNNING, RunState.WAITING_APPROVAL, RunState.SUSPENDED)
# Терминальные состояния: cancel к ним неприменим (409).
_TERMINAL_STATES = (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED)
# Сообщений истории сессии в контексте run'а (как _CHAT_HISTORY_LIMIT в CLI-chat).
_SESSION_HISTORY_LIMIT = 24
# TTL кэша каталога моделей провайдера: список моделей у провайдера меняется,
# но не на каждый запрос — 10 минут достаточно, чтобы не дёргать сеть на
# каждое открытие селектора модели.
CATALOG_TTL_SEC = 600.0
# TTL отрицательного кэша (неудачный fetch): короче успешного — недоступность
# провайдера обычно временная (сеть, рестарт), и через минуту стоит попробовать
# снова, а не ждать все 10 минут. Без него `send_message` (который зовёт
# `_derive` дважды на сообщение: сам и снова внутри `_acquire_warm`) при
# недоступном провайдере бьёт по сети под `_warm_lock` на каждое сообщение,
# сериализуя все чат-сессии процесса за время его недоступности.
CATALOG_NEGATIVE_TTL_SEC = 60.0


class CancelNotAllowedError(Exception):
    """Run уже терминален — отменять нечего (ADR-0017 §2)."""


class SessionBusyError(Exception):
    """В сессии есть незавершённый run — удалять её нельзя."""


class UnknownProviderError(Exception):
    """Провайдер не описан в models.providers — наружу как HTTP 404."""


class MemoryDisabledError(Exception):
    """Память не настроена в конфиге — экрана памяти быть не может."""


class MemoryPathError(Exception):
    """Путь страницы вне memory/ или не markdown."""


@dataclass
class _WarmSlot:
    """Тёплый sandbox сессии: env/infra/MCP живут между сообщениями (ADR-0017).

    Runner хранится вместе с ресурсами: env смонтирован на его workspace,
    и все сообщения сессии обязаны идти через один и тот же runner.
    """

    workspace: Path
    runner: TaskRunner
    resources: SessionResources
    last_used: float
    override: RunOverride = RunOverride()
    # Автономия, под которой поднят слот: policy-мост внешнего агента живёт в
    # ресурсах слота, и сообщение с другой автономией обязано пересобрать их —
    # иначе смена supervised → yolo в композере не долетает до policy (гейты
    # продолжают спрашивать, находка 2026-07-30).
    autonomy: AutonomyMode = AutonomyMode.YOLO


@dataclass
class _RunHolder:
    """Мутабельный держатель run_id: on_run_started заполняет его до прочих хуков."""

    run_id: str | None = None


@dataclass
class GatewayService:
    cfg: SvarogConfig
    workspace: Path
    events: EventStream = field(default_factory=InProcessEventStream)
    # Канал событий сессий для WS /sessions/events (спека 2026-08-05):
    # WorkspaceHub передаёт общий hub всем корням, TenantHub оставляет
    # per-tenant дефолт — жильцы не видят чужих названий.
    session_events: SessionEventHub = field(default_factory=SessionEventHub)
    # Колбэк на создание run'а — TenantHub пишет им run_index run→tenant (ADR-0014).
    on_run_created: Callable[[str], None] | None = None
    # Колбэк на создание сессии — WorkspaceHub пишет им session→root
    # в реестр маршрутизации (спека 2026-07-30).
    on_session_created: Callable[[str], None] | None = None
    # Роль тенанта (ADR-0013): фиксируется в runner'е и держит кламп на resume.
    role: TenantRole = TenantRole.SUPERUSER
    # Проверка квоты перед стартом run'а — TenantHub вешает сюда лимиты тенанта
    # (ADR-0014, Фаза 3); бросает QuotaExceededError. None — без квот.
    quota_guard: Callable[[], Awaitable[None]] | None = None
    # Идентичность для /whoami (ADR-0017 §2); TenantHub проставляет tenant_id.
    tenant_id: str = "local"
    # Куда вкладка MCP пишет новые серверы (спека 2026-08-06). WorkspaceHub и
    # локальный serve ставят сюда ~/.svarog/svarog.yaml: MCP подключается к
    # самому Сварогу, а не к рабочей папке, и виден во всех корнях сразу.
    # TenantHub оставляет None — этот файл принадлежит оператору хоста, и
    # запись туда подняла бы серверы одного жильца всем остальным.
    user_config_path: Path | None = None
    # Правку общего пользовательского слоя надо донести до сервисов остальных
    # корней: WorkspaceHub вешает сюда перечитывание всех своих сервисов.
    on_user_config_written: Callable[[], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        self._runner = TaskRunner(self.cfg, self.workspace, role=self.role)
        # Держим ссылки на фоновые задачи, чтобы их не собрал GC (RUF006).
        self._tasks: set[asyncio.Task[None]] = set()
        # Супервизор refuel (§6.10): счётчик авто-resume'ов на run (предохранитель)
        # и множество run'ов с уже запущенным авто-возобновлением (без гонки).
        self._auto_resumes: dict[str, int] = {}
        self._inflight: set[str] = set()
        # Retention-GC task-workspace'ов (ADR-0017): троттлинг по monotonic.
        self._last_gc = 0.0
        # Тёплые sandbox'ы сессий gateway-chat: session_id → слот; создание
        # сериализовано локом (двойной слот = утёкший контейнер).
        self._warm: dict[str, _WarmSlot] = {}
        self._warm_lock = asyncio.Lock()
        # Каталоги моделей: имя провайдера → (момент загрузки, карточки).
        # TTL, а не вечный кэш: список моделей у провайдера меняется.
        self._catalog: dict[str, tuple[float, list[ModelCard]]] = {}
        # Отрицательный кэш: имя провайдера → (момент неудачи, текст ошибки).
        # Короткий TTL (CATALOG_NEGATIVE_TTL_SEC) — см. его комментарий.
        self._catalog_failures: dict[str, tuple[float, str]] = {}

    # --- per-run workspaces (ADR-0017) ------------------------------------

    def _runner_for(
        self,
        workspace: Path,
        *,
        cfg: SvarogConfig | None = None,
        run_meta: dict[str, object] | None = None,
        allow_overlap: bool = False,
    ) -> TaskRunner:
        """Runner для workspace run'а; workspace сервиса — общий self._runner.

        Per-run runner делит с сервисом конфиг (та же БД/память/секреты
        тенанта) и отличается только рабочим деревом — изоляция путей ядра
        уже параметризована по workspace (ADR-0012). Общий runner переиспользуется
        только когда нет ни override-конфига, ни run_meta — иначе у run'а
        своя, производная конфигурация (override сообщения, задача 1).

        allow_overlap — сессия с принятым пересечением control-plane
        (ADR-0018): runner строится со своим флагом и общий self._runner
        (собранный без флага) не переиспользуется.
        """
        ws = workspace.expanduser().resolve()
        # apply_override возвращает тот же объект, если override пуст (overrides.py):
        # cfg is self.cfg означает «производной конфигурации нет», как и cfg is None.
        if (
            (cfg is None or cfg is self.cfg)
            and run_meta is None
            and not allow_overlap
            and ws == self.workspace.expanduser().resolve()
        ):
            return self._runner
        return TaskRunner(
            cfg or self.cfg,
            ws,
            role=self.role,
            run_meta=run_meta,
            allow_layout_overlap=allow_overlap,
        )

    async def _provision_workspace(
        self, task: str, repo: RepoSpec | None, name: str | None
    ) -> Path:
        """Workspace будущего run'а: named / git-клон / workspace сервиса."""
        if name is not None:
            path = resolve_named_workspace(self.workspace, name).resolve()
            # Ранний отказ 409 до docker/LLM; авторитетный lease-гард всё равно
            # срабатывает в run_once (ADR-0015 §0.5) — тут только быстрый UX.
            if await self._workspace_busy(path):
                raise WorkspaceBusyError(f"workspace '{name}' занят активным run")
            return path
        if repo is not None:
            dest = task_workspace_dir(self.workspace, task)
            credentials = self._git_credentials(repo.credentials_ref)
            await provision_clone(repo.url, dest, ref=repo.ref, credentials=credentials)
            return dest.resolve()
        return self.workspace

    def _git_credentials(self, ref: str | None) -> str | None:
        """Git-credentials из tenant-store (ADR-0017 развилка 3), только host-side.

        Явно названный ref обязан существовать; конвенциональный
        "git.credentials" опционален (нет секрета — анонимный clone).
        """
        store = self._runner.store  # tenant-скоуп (для standard — без env-fallback)
        if ref is not None:
            value = store.get(ref)
            if not value:
                raise CloneError(f"секрет '{ref}' (credentials_ref) не найден в tenant-store")
            return value
        return store.get(DEFAULT_GIT_CREDENTIALS_REF) or None

    async def _parallel_worktree(self, session: Session, workspace: Path) -> Path:
        """Перевести чат в собственный git-worktree, когда папка занята другим.

        Параллельные чаты в ОДНОЙ папке (31.07.2026): per-workspace lease
        (ADR-0015 §0.5) остаётся законом — параллельность достигается тем, что
        у каждого чата своё рабочее дерево. Worktree — сосед папки
        (`<parent>/.worktrees/<имя>-chat-<sid>`, конвенция child-run'ов §0.2),
        своя ветка `svarog/chat-<sid>`: результаты идут в неё, слияние — обычным
        git-flow (svarog push / merge). Не git, пустой репозиторий или занят
        уже СВОЙ worktree — прежний честный отказ.
        """
        meta = dict(session.meta or {})
        sid = session.id[:8]
        if meta.get("chat_worktree"):
            raise WorkspaceBusyError(f"в сессии {sid} ещё выполняется предыдущий run")
        repo = GitRepo(workspace)
        toplevel = await repo.toplevel()
        if (
            toplevel is None
            or toplevel.resolve() != workspace.resolve()
            or not await repo.has_commits()
        ):
            raise WorkspaceBusyError(
                f"папка занята run'ом другого чата, а параллельная работа в ней "
                f"возможна только для git-репозитория с коммитами: "
                f"{workspace} — дождитесь завершения или откройте другую папку"
            )
        worktree = workspace.parent / ".worktrees" / f"{workspace.name}-chat-{sid}"
        if not worktree.is_dir():
            # Уникальный суффикс ветки: ветка прошлой жизни чата (meta
            # потеряна/сессия пересоздана) не должна блокировать add -b.
            branch = f"svarog/chat-{sid}-{uuid.uuid4().hex[:4]}"
            await repo.add_worktree(worktree, branch)
        meta["chat_worktree"] = str(worktree)
        meta["workspace"] = str(worktree)

        async def persist(db: AsyncSession) -> None:
            found = await find_session_by_prefix(db, session.id)
            found.meta = meta
            await db.commit()

        await self._read(persist)
        session.meta = meta
        return worktree

    async def _workspace_busy(self, path: Path) -> bool:
        """Есть ли живой run в workspace (lease-семантика ADR-0015 §0.5)."""

        async def action(db: AsyncSession) -> bool:
            try:
                await TraceRecorder(db).acquire_workspace_lease(str(path))
            except WorkspaceBusyError:
                return True
            return False

        return await self._read(action)

    # --- тёплые sandbox'ы сессий (ADR-0017) --------------------------------

    async def _acquire_warm(
        self,
        session_id: str,
        workspace: Path,
        autonomy: AutonomyMode,
        override: RunOverride = RunOverride(),
        allow_overlap: bool = False,
    ) -> _WarmSlot | None:
        """Слот тёплого sandbox сессии; None — фича выключена (ttl=0).

        Первый вызов сессии поднимает env/infra/MCP один раз; дальнейшие
        сообщения переиспользуют их, экономя старт контейнера (~1.5-3s). Слот
        держит override, под которым он поднят: сообщение с тем же override
        переиспользует слот, а с другим — получает свежий (старый закрывается,
        иначе исполнитель или провайдер прошлого сообщения молча просочится
        в новое).
        """
        if self.cfg.cloud.warm_session_ttl_sec <= 0:
            return None
        async with self._warm_lock:
            slot = self._warm.get(session_id)
            if (
                slot is not None
                and slot.override == override
                and slot.autonomy == autonomy
                and slot.workspace == workspace
            ):
                # workspace в условии обязателен: сессия могла переехать в
                # свой worktree (параллельные чаты в одной папке) — env слота
                # смонтирован на ПРЕЖНЮЮ директорию.
                slot.last_used = time.monotonic()
                return slot
            if slot is not None:
                # Слот держит env/MCP, поднятые под прошлым конфигом ИЛИ прошлой
                # автономией (policy-мост агента зафиксировал её при подъёме):
                # с другим исполнителем, провайдером или режимом это чужой sandbox.
                await self._drop_warm(session_id)
            # Через _derive, а не голый apply_override: этот слот держит
            # runner, от cfg которого при старте посчитается config_hash —
            # он обязан совпасть с тем, что при resume соберёт _runner_for_run
            # (тоже через _derive), иначе _assert_config_unchanged откажет
            # resume'у (ADR-0015 §0.4).
            cfg, prices = await self._derive(override)
            run_meta = run_meta_for(override, prices)
            runner = self._runner_for(
                workspace, cfg=cfg, run_meta=run_meta, allow_overlap=allow_overlap
            )
            resources = await runner.prepare_session_resources(autonomy)
            slot = _WarmSlot(
                workspace=workspace,
                runner=runner,
                resources=resources,
                last_used=time.monotonic(),
                override=override,
                autonomy=autonomy,
            )
            self._warm[session_id] = slot
            return slot

    async def _drop_warm(self, session_id: str) -> None:
        """Закрыть и забыть тёплый слот (ошибка ноги / TTL / shutdown)."""
        slot = self._warm.pop(session_id, None)
        if slot is not None:
            await slot.resources.close()

    async def close_warm_sessions(self) -> None:
        """Закрыть все тёплые sandbox'ы (graceful shutdown, тесты)."""
        for session_id in list(self._warm):
            await self._drop_warm(session_id)

    async def _sweep_warm_sessions(self) -> None:
        """Закрыть тёплые слоты, простоявшие дольше TTL (idle-GC).

        Слот с живым run'ом (lease workspace) не трогаем: длинный run — не
        простой; его last_used обновится при следующем сообщении.
        """
        ttl = float(self.cfg.cloud.warm_session_ttl_sec)
        if ttl <= 0:
            return
        now = time.monotonic()
        for session_id, slot in list(self._warm.items()):
            if now - slot.last_used < ttl:
                continue
            if await self._workspace_busy(slot.workspace):
                continue
            await self._drop_warm(session_id)

    # --- каталог моделей и цены (задача 6) ---------------------------------

    def list_providers(self) -> list[ProviderView]:
        """Записи models.providers. Наружу — без api_key_ref (ADR-0006)."""
        return [
            ProviderView(
                name=name,
                base_url=provider.base_url,
                model=provider.model,
                is_default=name == self.cfg.models.default,
            )
            for name, provider in sorted(self.cfg.models.providers.items())
        ]

    def executor_options(self) -> list[ExecutorOption]:
        """Варианты исполнителя по текущему конфигу и наличию CLI/образов."""
        return executor_options(self.cfg)

    def sandbox_options(self) -> list[SandboxOption]:
        """Варианты sandbox по текущему конфигу и наличию docker-runtime."""
        return sandbox_options(self.cfg)

    async def provider_models(self, name: str) -> list[ModelCard]:
        """Список моделей провайдера с TTL-кэшем; CatalogError → 502.

        Неудача кэшируется тоже (короткий TTL, `CATALOG_NEGATIVE_TTL_SEC`):
        иначе повторный вызов при недоступном провайдере не находит хита и
        бьёт по сети заново, под тем же `_warm_lock`, что и все остальные
        сессии процесса (задача 3, финал ревью). Хит отрицательного кэша
        поднимает тот же `CatalogError`, что и реальный fetch, — поведение
        502 у эндпоинта не меняется, пустой список наружу не уходит.
        """
        provider = self.cfg.models.providers.get(name)
        if provider is None:
            raise UnknownProviderError(f"провайдер '{name}' не описан в models.providers")
        now = time.monotonic()
        cached = self._catalog.get(name)
        if cached is not None and now - cached[0] < CATALOG_TTL_SEC:
            return cached[1]
        failed = self._catalog_failures.get(name)
        if failed is not None and now - failed[0] < CATALOG_NEGATIVE_TTL_SEC:
            raise CatalogError(failed[1])
        api_key = resolve_api_key(provider, self._runner.host_store)
        try:
            cards = await fetch_models(provider, None if api_key == "not-needed" else api_key)
        except CatalogError as exc:
            self._catalog_failures[name] = (now, str(exc))
            raise
        self._catalog[name] = (now, cards)
        self._catalog_failures.pop(name, None)
        return cards

    async def check_provider(self, name: str) -> ProviderCheckView:
        """Живая проверка `/models` — мимо кэша каталога.

        Кэш хранит и отрицательные результаты (CATALOG_NEGATIVE_TTL_SEC), а
        «Проверить» обязан отражать состояние сейчас — поэтому fetch_models
        зовётся напрямую, без чтения и записи кэша.
        """
        provider = self.cfg.models.providers.get(name)
        if provider is None:
            raise UnknownProviderError(f"провайдер '{name}' не описан в models.providers")
        try:
            api_key = resolve_api_key(provider, self._runner.host_store)
            cards = await fetch_models(provider, None if api_key == "not-needed" else api_key)
        except (CatalogError, ApiKeyError) as exc:
            return ProviderCheckView(ok=False, error=str(exc))
        return ProviderCheckView(ok=True, models_count=len(cards))

    async def scan_models(self, base_url: str, api_key: str | None = None) -> list[ModelCard]:
        """Каталог `/models` ещё не сохранённого провайдера (форма настроек).

        Без кэша: скан — явное действие человека по свежевведённому URL,
        отдавать вчерашний список или кэшировать чужой ключ тут нечем и
        незачем. Ключ живёт только в теле запроса и заголовке к провайдеру.
        """
        if not base_url.startswith(("http://", "https://")):
            raise CatalogError("base_url должен начинаться с http(s)://")
        probe = ProviderConfig(base_url=base_url.rstrip("/"), model="scan")
        return await fetch_models(probe, api_key or None)

    async def _model_prices(self, provider: str, model: str) -> tuple[float, float] | None:
        """Цены модели из каталога; каталог недоступен — цены из конфига."""
        try:
            cards = await self.provider_models(provider)
        except (UnknownProviderError, CatalogError, ApiKeyError):
            return None
        for card in cards:
            if card.id == model:
                if card.input_usd_per_mtok is None or card.output_usd_per_mtok is None:
                    return None
                return (card.input_usd_per_mtok, card.output_usd_per_mtok)
        return None

    async def _derive(
        self, override: RunOverride
    ) -> tuple[SvarogConfig, tuple[float, float] | None]:
        """Производный конфиг сообщения вместе с ценами выбранной модели.

        Цены возвращаются отдельно от cfg, чтобы вызывающая сторона могла
        записать их в Run.meta (`run_meta_for`, задача 2) — resume обязан
        пережить их как есть, а не пересчитывать через каталог заново.
        """
        prices = None
        if override.model is not None:
            target = override.provider or self.cfg.models.default
            prices = await self._model_prices(target, override.model)
        return apply_override(self.cfg, override, prices=prices), prices

    # --- запуск и возобновление runs -------------------------------------

    async def usage(self) -> QuotaUsage:
        """Снимок использования по БД тенанта (для квот, ADR-0014 Фаза 3)."""

        async def action(db: AsyncSession) -> QuotaUsage:
            active, cost, tokens = await run_usage_totals(db)
            return QuotaUsage(active_runs=active, total_cost_usd=cost, total_tokens=tokens)

        return await self._read(action)

    async def create_run(
        self,
        task: str,
        autonomy: AutonomyMode | None,
        *,
        repo: RepoSpec | None = None,
        workspace_name: str | None = None,
    ) -> str:
        """Запустить run в фоне; вернуть run_id, как только он создан.

        Источник workspace (ADR-0017): git-клон в одноразовый task-workspace
        (`repo`), постоянный named workspace (`workspace_name`) либо workspace
        сервиса. Квота проверяется ДО клона (429 раньше сетевой работы).
        """
        if self.quota_guard is not None:
            await self.quota_guard()  # QuotaExceededError → 429 на транспорте
        workspace = await self._provision_workspace(task, repo, workspace_name)
        mode = autonomy if autonomy is not None else self.cfg.runtime.autonomy
        started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._spawn(self._run_bg(task, mode, started, runner=self._runner_for(workspace)))
        return await started

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_bg(
        self,
        task: str,
        autonomy: AutonomyMode,
        started: asyncio.Future[str],
        *,
        runner: TaskRunner | None = None,
        session_id: str | None = None,
        history: list[ChatMessage] | None = None,
        warm: _WarmSlot | None = None,
    ) -> None:
        holder = _RunHolder()
        hooks = self._event_hooks(holder, started)
        try:
            outcome = await (runner or self._runner).run_once(
                task,
                autonomy,
                hooks=hooks,
                session_id=session_id,
                history=history,
                resources=warm.resources if warm is not None else None,
            )
            self._publish_finished(outcome)
            self._spawn(self._autotitle_bg(outcome.run_id, outcome.final_answer))
        except Exception as exc:
            if warm is not None and session_id is not None:
                # Нога упала — sandbox может быть в неизвестном состоянии
                # (умерший контейнер и т.п.); следующий message поднимет свежий.
                await self._drop_warm(session_id)
            self._publish_error(holder, started, exc)

    async def resume_run(self, run_id: str) -> None:
        """Возобновить run в фоне (после решения approval / из suspended)."""
        # Новая нога стримит с чистого листа: старый run_finished не должен
        # обрывать подписчика, подключившегося после возобновления.
        self.events.reset(run_id)
        self._spawn(self._resume_bg(run_id))

    async def _resume_bg(self, run_id: str) -> None:
        holder = _RunHolder(run_id=run_id)
        started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        started.set_result(run_id)
        hooks = self._event_hooks(holder, started)
        try:
            # Runner — по workspace run'а (per-run workspaces, ADR-0017):
            # resume под конфигом тенанта, а не сервиса-по-умолчанию.
            runner = await self._runner_for_run(run_id)
            outcome = await runner.resume(run_id, hooks=hooks)
            self._publish_finished(outcome)
            self._spawn(self._autotitle_bg(outcome.run_id, outcome.final_answer))
        except Exception as exc:
            self._publish_error(holder, started, exc)

    async def _runner_for_run(self, run_id: str) -> TaskRunner:
        """Runner для resume: workspace, override и цены читаются из строки run'а.

        Без восстановления override дайджест конфига разойдётся со снимком
        старта, и `_assert_config_unchanged` отклонит resume (ADR-0015 §0.4).

        Цены — из Run.meta, не из каталога: `_derive` здесь намеренно не
        вызывается, чтобы resume не бил по сети провайдера. Если бы цены
        пересчитывались заново, недоступный к моменту resume провайдер (TTL
        кэша истёк, `write_config` его очистил, сеть упала) молча откатывал
        бы стоимость run'а на цены из `svarog.yaml` для другой модели того
        же провайдера — а approval-гейт — это всегда resume (задача 2).
        """

        async def action(db: AsyncSession) -> tuple[str | None, dict[str, object], bool]:
            run = await find_run_by_prefix(db, run_id)
            # Согласие на пересечение с control-plane — свойство сессии
            # (ADR-0018): resume обязан собрать runner с тем же флагом, что и
            # старт, иначе одобренный run упадёт на гейте раскладки.
            session = await db.get(Session, run.session_id)
            allow = bool(((session.meta if session else None) or {}).get("allow_overlap"))
            return run.workspace, dict(run.meta or {}), allow

        workspace, meta, allow_overlap = await self._read(action)
        override = RunOverride.from_meta(meta)
        cfg = (
            apply_override(self.cfg, override, prices=prices_from_meta(meta))
            if not override.is_empty()
            else None
        )
        if not workspace:
            if cfg is None and not allow_overlap:
                return self._runner
            return self._runner_for(self.workspace, cfg=cfg, allow_overlap=allow_overlap)
        return self._runner_for(Path(workspace), cfg=cfg, allow_overlap=allow_overlap)

    # --- события ----------------------------------------------------------

    def _event_hooks(self, holder: _RunHolder, started: asyncio.Future[str]) -> RunHooks:
        def on_started(run: Run) -> None:
            holder.run_id = run.id
            # run_index run→tenant (ADR-0014): идемпотентно, безопасно и на resume.
            if self.on_run_created is not None:
                self.on_run_created(run.id)
            if not started.done():
                started.set_result(run.id)

        def emit(event: dict[str, Any]) -> None:
            if holder.run_id is not None:
                self.events.publish(holder.run_id, event)

        def on_check(check: CheckOutcome) -> None:
            emit({"type": "check", "name": check.name, "status": check.status.value})

        return RunHooks(
            on_run_started=on_started,
            on_text_delta=lambda delta: emit({"type": "text", "delta": delta}),
            on_tool_call=lambda name, args: emit(
                {
                    "type": "tool_call",
                    "tool": name,
                    "arg": short_arg(args, workspace=self.workspace),
                }
            ),
            on_tool_result=lambda name, status, summary: emit(
                {"type": "tool_result", "tool": name, "status": status, "result": summary}
            ),
            # Живой прогресс (токены/стоимость): у внешнего executor'а — с
            # ticker'а bridge-прокси, у нативного loop — после каждой итерации.
            on_progress=lambda iterations, tokens, cost, _ctx, _cached: emit(
                {
                    "type": "progress",
                    "iterations": iterations,
                    "tokens": tokens,
                    "cost_usd": cost,
                }
            ),
            on_phase=lambda text: emit({"type": "phase", "text": text}),
            on_notify=lambda name, reason: emit({"type": "notify", "tool": name, "reason": reason}),
            on_check=on_check,
            on_commit=lambda sha, branch, push: emit(
                {"type": "commit", "sha": sha, "branch": branch}
            ),
            # Гейт появляется в ленте сразу, а не по опросу /approvals.
            # Нативный цикл зовёт on_approval_created, внешний агент —
            # on_approval_requested: подключены оба, событие одно.
            on_approval_created=lambda approval: emit(
                {
                    "type": "approval_required",
                    "approval_id": approval.id,
                    "action_type": approval.action_type,
                    "payload": approval.payload or {},
                }
            ),
            on_approval_requested=lambda approval: emit(
                {
                    "type": "approval_required",
                    "approval_id": approval.id,
                    "action_type": approval.action_type,
                    "payload": approval.payload or {},
                }
            ),
        )

    def _publish_finished(self, outcome: RunOutcome) -> None:
        self.events.publish(
            outcome.run_id,
            {
                "type": "run_finished",
                "run_id": outcome.run_id,
                "state": outcome.state.value,
                "final_answer": outcome.final_answer,
                "error": outcome.error,
            },
        )

    async def _autotitle_draft_bg(self, session_id: str, task_text: str) -> None:
        """Черновик названия по одному вопросу (спека 2026-08-05): best-effort.

        Сбой модели -> fallback-обрезка вопроса: что-то осмысленное появляется
        в сайдбаре сразу, а уточнение после ответа всё равно попробует лучше.
        """
        try:
            if not task_text.strip():
                return

            async def read(db: AsyncSession) -> bool:
                session = await db.get(Session, session_id)
                return session is not None and needs_draft(session.title, session.meta)

            if not await self._read(read):
                return
            generated = await title_for(
                lambda: auxiliary_provider(
                    self.cfg.models, default_secret_store(self.cfg.secrets.path)
                ),
                task_text,
                "",
            )
            draft = generated or fallback_title(task_text)
            if draft is None:
                return
            draft_title: str = draft

            async def write(db: AsyncSession) -> bool:
                session = await db.get(Session, session_id)
                if session is None or not needs_draft(session.title, session.meta):
                    return False  # гонка: быстрое уточнение успело раньше
                session.title = draft_title
                # JSON-колонка без MutableDict: только присваивание нового dict.
                session.meta = {
                    **(session.meta or {}),
                    "autotitle": "draft",
                    "autotitle_draft": draft_title,
                }
                await db.commit()
                return True

            if await self._read(write):
                self.session_events.publish(
                    {
                        "type": "session_title",
                        "session_id": session_id,
                        "title": draft_title,
                        "phase": "draft",
                    }
                )
        except Exception:
            logger.warning("автоназвание: черновик не удался", exc_info=True)
            return

    async def _autotitle_bg(self, run_id: str, answer: str) -> None:
        """Уточнение названия чата после ответа (спека 2026-08-05): best-effort.

        Отдельная фоновая задача после run_finished: сбой модели или БД не
        влияет на run. done/fallback окончательны; черновик, переименованный
        вручную (CLI), не перетирается — это решает needs_refine.

        had_draft и текущий title решаются ВНУТРИ write, по свежепрочитанной
        строке, а не в read до вызова aux-модели: черновик мог появиться,
        пока aux-вызов уточнения был в полёте (фоновая задача черновика —
        отдельный таск), и стухший снимок из read увёл бы сбой уточнения в
        fallback-обрезку поверх уже хорошего названия черновика (ревью
        задачи 3, 2026-08-05).
        """
        try:

            async def read(db: AsyncSession) -> tuple[str, str] | None:
                run = await db.get(Run, run_id)
                if run is None:
                    return None
                session = await db.get(Session, run.session_id)
                if session is None or not needs_refine(session.title, session.meta):
                    return None
                first = (
                    await db.execute(
                        select(Run.task)
                        .where(Run.session_id == session.id)
                        .order_by(Run.created_at, Run.id)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                return session.id, first or ""

            found = await self._read(read)
            if found is None:
                return
            session_id, first_task = found
            if not first_task.strip():
                return
            generated = await title_for(
                lambda: auxiliary_provider(
                    self.cfg.models, default_secret_store(self.cfg.secrets.path)
                ),
                first_task,
                answer,
            )

            async def write(db: AsyncSession) -> tuple[str, bool] | None:
                # Черновик (_autotitle_draft_bg) и это уточнение пишут в свою
                # транзакцию независимо; если их записи пересекутся по
                # времени — last-writer-wins на уровне БД. Окно в
                # микросекунды, а исход косметический (текст заголовка),
                # поэтому отдельная блокировка тут избыточна.
                session = await db.get(Session, session_id)
                if session is None or not needs_refine(session.title, session.meta):
                    return None  # гонка: параллельный run уже уточнил
                had_draft = (session.meta or {}).get("autotitle") == "draft"
                if generated is not None:
                    final_title, flag = generated, "done"
                elif had_draft:
                    # Модель упала, но черновик уже стоит (свежий, не снимок
                    # из read) — он лучше обрезки.
                    final_title, flag = session.title or "", "done"
                else:
                    fb = fallback_title(first_task)
                    if fb is None:
                        return None
                    final_title, flag = fb, "fallback"
                changed = (session.title or "") != final_title
                session.title = final_title
                # JSON-колонка без MutableDict: только присваивание нового dict.
                session.meta = {**(session.meta or {}), "autotitle": flag}
                await db.commit()
                return final_title, changed

            # _read — обёртка with_db и годится и для записи (историческое имя).
            result = await self._read(write)
            if result is not None and result[1]:
                final_title, _ = result
                self.session_events.publish(
                    {
                        "type": "session_title",
                        "session_id": session_id,
                        "title": final_title,
                        "phase": "final",
                    }
                )
        except Exception:
            # Автоназвание никогда не роняет фоновую задачу (best-effort, спека).
            logger.warning("автоназвание: фоновая задача не удалась", exc_info=True)
            return

    def _publish_error(
        self, holder: _RunHolder, started: asyncio.Future[str], exc: Exception
    ) -> None:
        if not started.done():
            started.set_exception(exc)
            return
        if holder.run_id is not None:
            self.events.publish(
                holder.run_id,
                {
                    "type": "run_finished",
                    "state": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )

    def stream(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        """Асинхронный итератор событий run'а (история + живые)."""
        return self.events.stream(run_id)

    async def wait_for_background(self) -> None:
        """Дождаться завершения фоновых run/resume-задач (graceful shutdown, тесты)."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    # --- чтение trace -----------------------------------------------------

    async def _read[T](self, action: Callable[[AsyncSession], Awaitable[T]]) -> T:
        return await self._runner.with_db(action)

    async def list_runs(self, limit: int = 20) -> list[RunSummary]:
        async def action(db: AsyncSession) -> list[RunSummary]:
            return [_summary(run) for run in await fetch_runs(db, limit=limit)]

        return await self._read(action)

    async def get_run(self, run_id: str) -> RunDetail:
        async def action(db: AsyncSession) -> RunDetail:
            run, messages, tool_calls, checks = await fetch_run(db, run_id)
            return RunDetail(
                **_summary(run).model_dump(),
                messages=[{"role": m.role, "index": m.index_in_run, **m.content} for m in messages],
                tool_calls=[
                    ToolCallView(
                        tool_name=c.tool_name,
                        risk_level=c.risk_level,
                        policy_decision=c.policy_decision,
                        status=c.status.value,
                        error=c.error,
                    )
                    for c in tool_calls
                ],
                checks=[{"name": c.check_name, "status": c.status.value} for c in checks],
            )

        return await self._read(action)

    async def list_pending_approvals(self) -> list[ApprovalView]:
        async def action(db: AsyncSession) -> list[ApprovalView]:
            approvals = await TraceRecorder(db).fetch_pending_approvals()
            return [
                ApprovalView(
                    approval_id=a.id,
                    run_id=a.run_id,
                    action_type=a.action_type,
                    payload=a.payload or {},
                )
                for a in approvals
            ]

        return await self._read(action)

    async def decide_approval(self, approval_id: str, *, approved: bool, reason: str | None) -> str:
        """Записать решение человека; вернуть run_id для возобновления (ADR-0005)."""

        async def action(db: AsyncSession) -> str:
            recorder = TraceRecorder(db)
            approval = await recorder.find_approval_by_prefix(approval_id)
            await recorder.decide_approval(
                approval, approved=approved, decided_by="api", reason=reason
            )
            return approval.run_id

        return await self._read(action)

    async def answer_question(self, approval_id: str, *, answer: str) -> str:
        """Записать текстовый ответ на ask_user; вернуть run_id (§6.5)."""

        async def action(db: AsyncSession) -> str:
            recorder = TraceRecorder(db)
            approval = await recorder.find_approval_by_prefix(approval_id)
            await recorder.answer_question(approval, answer=answer, answered_by="api")
            return approval.run_id

        return await self._read(action)

    async def cancel_run(self, run_id: str) -> CancelView:
        """Cooperative-cancel (ADR-0017 §2).

        Run без живой ноги (waiting_approval/suspended/протухший) терминализируется
        сразу — его pending-approvals закрываются отказом. Живой RUNNING получает
        флаг в meta: loop завершит run на границе итерации, checkpoint сохранён.
        """

        async def action(db: AsyncSession) -> CancelView:
            recorder = TraceRecorder(db)
            run = await find_run_by_prefix(db, run_id)
            if run.state in _TERMINAL_STATES:
                raise CancelNotAllowedError(
                    f"run {run.id[:8]} уже в терминальном состоянии '{run.state.value}'"
                )
            if run.state == RunState.RUNNING:
                await recorder.request_cancel(run)
                return CancelView(run_id=run.id, state="cancelling")
            # waiting_approval / suspended / pending: живой ноги нет —
            # терминализируем сразу и закрываем pending-approvals отказом.
            for approval in await recorder.fetch_pending_approvals():
                if approval.run_id == run.id:
                    await recorder.decide_approval(
                        approval, approved=False, decided_by="cancel", reason="run отменён"
                    )
            await recorder.finish_run(run, RunState.CANCELLED)
            return CancelView(run_id=run.id, state="cancelled")

        view = await self._read(action)
        if view.state == "cancelled":
            self.events.publish(
                view.run_id,
                {"type": "run_finished", "run_id": view.run_id, "state": "cancelled"},
            )
        return view

    async def whoami(self) -> WhoamiView:
        """Идентичность и usage тенанта (ADR-0017 §2)."""
        usage = await self.usage()
        return WhoamiView(
            tenant_id=self.tenant_id,
            role=self.role.value,
            active_runs=usage.active_runs,
            total_cost_usd=usage.total_cost_usd,
            total_tokens=usage.total_tokens,
        )

    # --- сессии gateway-chat (ADR-0017 §2, семантика §10.1) ---------------

    async def create_session(
        self,
        *,
        title: str = "",
        repo: RepoSpec | None = None,
        workspace_name: str | None = None,
        accept_overlap: bool = False,
    ) -> SessionView:
        """Сессия: workspace провижнится один раз и живёт всю серию runs."""
        workspace = await self._provision_workspace(title or "session", repo, workspace_name)
        workspace_str = str(workspace.expanduser().resolve())
        meta: dict[str, object] = {
            "workspace": workspace_str,
            # root — корень сервиса, обработавшего запрос: для path-сессий
            # это выбранный корень (сервис создан WorkspaceHub.service_for
            # ровно под него), для repo/named — root дефолтного сервиса.
            # workspace — где физически работает агент (clone/task-каталог
            # внутри этого root); для repo/named они не совпадают, поэтому
            # заведено отдельное поле (спека 2026-07-30, финальное ревью).
            "root": str(self.workspace.expanduser().resolve()),
        }
        if accept_overlap:
            # Человек принял пересечение workspace с control-plane в пикере
            # (ADR-0018): все runs сессии пойдут с allow_layout_overlap.
            # Флаг живёт в meta сессии — согласие раз на сессию, не на run.
            meta["allow_overlap"] = True

        async def action(db: AsyncSession) -> Session:
            return await TraceRecorder(db).create_session(
                title=title or "gateway-сессия", meta=meta
            )

        session = await self._read(action)
        if self.on_session_created is not None:
            self.on_session_created(session.id)
        return SessionView(
            session_id=session.id, title=session.title or "", workspace=workspace_str, runs=[]
        )

    async def send_message(
        self,
        session_id: str,
        text: str,
        autonomy: AutonomyMode | None,
        override: RunOverride = RunOverride(),
        attachments: Sequence[str] = (),
    ) -> str:
        """Сообщение чата → отдельный run в workspace сессии с её историей.

        Контекст диалога — по типу executor'а (как в CLI-chat): нативному loop
        передаётся history из trace; внешний агент (ADR-0016) продолжает
        собственную сессию по agent_session_id, history ему не нужна —
        run_once сам резолвит agent_session по session_id.

        `override` — выбор в поле ввода (задача 3), а не правка svarog.yaml:
        производный конфиг строится до проверок занятости workspace, чтобы
        негодный override отвечал 422 раньше, чем занятость — 409.

        `attachments` — относительные пути из `.attachments/` этой сессии
        (задача 7); проверяются сразу после резолва workspace, до захвата
        lease — негодный путь отвечает 400 раньше, чем стоит занятость.
        """
        if self.quota_guard is not None:
            await self.quota_guard()  # QuotaExceededError → 429
        cfg, prices = await self._derive(override)  # OverrideError → 422
        external = cfg.executor.type == "external"

        async def action(db: AsyncSession) -> tuple[Session, list[dict[str, str]]]:
            session = await find_session_by_prefix(db, session_id)
            if external:
                return session, []
            raw = await TraceRecorder(db).session_history(
                session.id, limit_messages=_SESSION_HISTORY_LIMIT
            )
            return session, raw

        session, raw = await self._read(action)

        async def touch(db: AsyncSession) -> None:
            # updated_at меняется только при UPDATE строки Session, а run'ы её
            # не трогают: без этого навигатор сортировал бы по времени
            # создания, и вчерашняя сессия с сообщением минуту назад падала
            # бы в «Ранее» с холодной шкалой накала.
            found = await find_session_by_prefix(db, session.id)
            found.updated_at = datetime.now(UTC).replace(tzinfo=None)
            await db.commit()

        await self._read(touch)
        workspace = Path((session.meta or {}).get("workspace") or self.workspace)
        if not workspace.is_dir():
            raise UnknownWorkspaceError(
                f"workspace сессии {session.id[:8]} больше не существует: {workspace}"
            )
        if attachments:
            for rel in attachments:
                verify_attachment(workspace, rel)  # AttachmentPathError → 400
            text = f"{text}\n\n{attachments_note(list(attachments))}"
        if await self._workspace_busy(workspace):
            # Папка занята run'ом ДРУГОГО чата → параллельность через
            # собственный git-worktree сессии; занят СВОЙ worktree (или папка
            # не git) — честный отказ, как раньше.
            workspace = await self._parallel_worktree(session, workspace)
        history = (
            None
            if external
            else [
                ChatMessage(
                    role="user" if m["role"] == "user" else "assistant", content=m["content"]
                )
                for m in raw
            ]
        )
        mode = autonomy if autonomy is not None else cfg.runtime.autonomy
        # Согласие на пересечение с control-plane — свойство сессии (ADR-0018,
        # пишется в meta при создании через пикер): раскатывается на каждый run.
        allow_overlap = bool((session.meta or {}).get("allow_overlap"))
        # Тёплый sandbox сессии (ADR-0017): env/infra/MCP переживают сообщение.
        run_meta = run_meta_for(override, prices)
        warm = await self._acquire_warm(
            session.id, workspace, mode, override, allow_overlap=allow_overlap
        )
        runner = (
            warm.runner
            if warm is not None
            else self._runner_for(
                workspace,
                cfg=cfg if not override.is_empty() else None,
                run_meta=run_meta,
                allow_overlap=allow_overlap,
            )
        )
        started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._spawn(
            self._run_bg(
                text,
                mode,
                started,
                runner=runner,
                session_id=session.id,
                history=history,
                warm=warm,
            )
        )
        # Черновик названия по одному вопросу (спека 2026-08-05): не ждёт
        # ни старта run'а, ни его завершения.
        self._spawn(self._autotitle_draft_bg(session.id, text))
        return await started

    async def get_session(self, session_id: str) -> SessionView:
        async def action(db: AsyncSession) -> SessionView:
            session = await find_session_by_prefix(db, session_id)
            result = await db.execute(
                select(Run).where(Run.session_id == session.id).order_by(Run.created_at)
            )
            runs = [_summary(run) for run in result.scalars()]
            return SessionView(
                session_id=session.id,
                title=session.title or "",
                workspace=(session.meta or {}).get("workspace"),
                runs=runs,
            )

        return await self._read(action)

    async def list_sessions(self, limit: int = 50) -> list[SessionSummary]:
        """Сессии для навигатора: свежие сверху, без полного трейса."""

        async def action(db: AsyncSession) -> list[SessionSummary]:
            found = await db.execute(
                select(Session).order_by(Session.updated_at.desc()).limit(limit)
            )
            summaries: list[SessionSummary] = []
            for session in found.scalars():
                runs = (
                    (
                        await db.execute(
                            select(Run).where(Run.session_id == session.id).order_by(Run.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                summaries.append(
                    SessionSummary(
                        session_id=session.id,
                        title=session.title or "",
                        workspace=(session.meta or {}).get("workspace"),
                        root=(session.meta or {}).get("root"),
                        updated_at=session.updated_at,
                        runs_count=len(runs),
                        last_state=runs[-1].state.value if runs else None,
                    )
                )
            return summaries

        return await self._read(action)

    async def delete_session(self, session_id: str) -> None:
        """Удалить сессию вместе с её runs (каскад в схеме, ADR-0015).

        Живую сессию не трогаем: удалить историю запуска, который прямо
        сейчас правит рабочее дерево, — способ потерять след того, что
        уже сделано.
        """

        async def action(db: AsyncSession) -> None:
            session = await find_session_by_prefix(db, session_id)
            live = (
                await db.execute(
                    select(Run)
                    .where(Run.session_id == session.id, Run.state.in_(_LIVE_STATES))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if live is not None:
                raise SessionBusyError(
                    "в этом чате ещё идёт запуск — дождитесь конца или прервите его"
                )
            await db.delete(session)
            await db.commit()

        await self._read(action)

    async def session_thread(self, session_id: str) -> SessionThread:
        """История сессии как лента: задача, вызовы, финальный ответ по каждому run."""

        async def action(db: AsyncSession) -> SessionThread:
            session = await find_session_by_prefix(db, session_id)
            runs = (
                (
                    await db.execute(
                        select(Run).where(Run.session_id == session.id).order_by(Run.created_at)
                    )
                )
                .scalars()
                .all()
            )
            recorder = TraceRecorder(db)
            # Живой run отдаётся ОТДЕЛЬНО от items: клиент переподписывается
            # на его WS, и реплей истории событий восстанавливает ленту —
            # включать его частичный трейс в items значило бы задвоить её
            # (параллельные чаты, 31.07.2026). Живость — как у lease:
            # RUNNING со свежим heartbeat, протухший не в счёт.
            # Run без workspace (легаси-строки, заведённые до колонки лизы) не
            # держит per-workspace лизу — спрашивать по нему занятость нечего.
            # Голый None сюда пускать нельзя: условие выродилось бы в
            # `workspace IS NULL` и поймало бы чужой run без workspace.
            last = runs[-1] if runs else None
            live = (
                await recorder.live_run_on_workspace(last.workspace)
                if last is not None and last.workspace is not None
                else None
            )
            live_run = (
                last
                if last is not None
                and live is not None
                and live.id == last.id
                and last.state == RunState.RUNNING
                else None
            )
            items: list[ThreadItemView] = []
            for run in runs:
                if live_run is not None and run.id == live_run.id:
                    continue
                items.append(ThreadItemView(kind="user", text=run.task))
                calls = (
                    (
                        await db.execute(
                            select(ToolCall)
                            .where(ToolCall.run_id == run.id)
                            .order_by(ToolCall.started_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                for call in calls:
                    server, _, bare = call.tool_name.rpartition("/")
                    items.append(
                        ThreadItemView(
                            kind="call",
                            server=server or None,
                            name=bare,
                            arg=short_arg(call.arguments or {}, workspace=self.workspace),
                            result=short_result(
                                ok=call.status is ToolCallStatus.SUCCEEDED,
                                output=str((call.result or {}).get("output", "")),
                                error=call.error,
                                workspace=self.workspace,
                            ),
                            status=call.status.value,
                        )
                    )
                answer = await recorder.last_assistant_text(run)
                if answer:
                    items.append(ThreadItemView(kind="say", text=answer))
            return SessionThread(
                session_id=session.id,
                title=session.title or "",
                items=items,
                live_run_id=live_run.id if live_run is not None else None,
                live_task=live_run.task if live_run is not None else None,
            )

        return await self._read(action)

    async def file_suggestions(self, session_id: str, query: str) -> list[Suggestion]:
        """Подсказки `@file` по workspace сессии.

        Корень — workspace именно сессии, а не сервиса: у сессии может быть
        своя рабочая папка (ADR-0017), и подсказки обязаны показывать те
        файлы, которые агент этой сессии действительно увидит.
        """

        async def action(db: AsyncSession) -> dict[str, object]:
            session = await find_session_by_prefix(db, session_id)
            return dict(session.meta or {})

        meta = await self._read(action)
        workspace = Path(str(meta.get("workspace") or self.workspace))
        token = query if query.startswith("@") else f"@{query}"
        return at_suggestions(workspace, token)

    async def store_attachment(self, session_id: str, name: str, data: bytes) -> StoredAttachment:
        """Положить вложение в workspace сессии; под живой запуск — отказ."""

        async def action(db: AsyncSession) -> tuple[str, dict[str, object]]:
            session = await find_session_by_prefix(db, session_id)
            live = (
                await db.execute(
                    select(Run)
                    .where(Run.session_id == session.id, Run.state.in_(_LIVE_STATES))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if live is not None:
                raise SessionBusyError(
                    "в этом чате идёт запуск — дождитесь конца, прежде чем прикреплять файлы"
                )
            return session.id, dict(session.meta or {})

        _, meta = await self._read(action)
        workspace = Path(str(meta.get("workspace") or self.workspace))
        return await store_attachment(workspace, name, data)

    async def attachment_path(self, session_id: str, name: str) -> Path:
        """Резолвит вложение сессии для раздачи назад (`GET .../attachments/{name}`).

        Тот же fail-closed резолв, что при приёме (`verify_attachment`) — путь
        строится и проверяется в одном месте, а не конкатенацией строк здесь.
        """

        async def action(db: AsyncSession) -> dict[str, object]:
            session = await find_session_by_prefix(db, session_id)
            return dict(session.meta or {})

        meta = await self._read(action)
        workspace = Path(str(meta.get("workspace") or self.workspace))
        return verify_attachment(workspace, f"{ATTACHMENTS_DIR}/{name}")

    # --- память (план 2026-07-27) ------------------------------------------

    def _memory_root(self) -> Path:
        root = memory_dir(self.cfg)
        if root is None or not root.is_dir():
            raise MemoryDisabledError("память не настроена: в конфиге нет memory.path")
        return root

    def _memory_file(self, rel_path: str) -> Path:
        """Абсолютный путь страницы; выход за пределы memory/ — отказ.

        Путь приходит от клиента, поэтому проверяется, а не склеивается:
        `../../.ssh/id_rsa` не должен читаться через веб (ADR-0012).
        """
        root = self._memory_root()
        target = (root / rel_path).resolve()
        if not target.is_relative_to(root) or target.suffix != ".md":
            raise MemoryPathError(f"недопустимый путь страницы памяти: {rel_path}")
        return target

    def memory_tree(self) -> list[MemoryPageView]:
        """Страницы памяти как они лежат в Git — markdown, отсортированные по пути."""
        root = self._memory_root()
        pages: list[MemoryPageView] = []
        for path in sorted(root.rglob("*.md")):
            stat = path.stat()
            pages.append(
                MemoryPageView(
                    path=str(path.relative_to(root)),
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                )
            )
        return pages

    def memory_file(self, rel_path: str) -> MemoryFileView:
        target = self._memory_file(rel_path)
        if not target.is_file():
            raise MemoryPathError(f"страницы памяти нет: {rel_path}")
        stat = target.stat()
        return MemoryFileView(
            path=rel_path,
            text=target.read_text(encoding="utf-8"),
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )

    async def memory_search(self, query: str, limit: int = 10) -> list[MemoryHitView]:
        """Поиск тем же механизмом, что у агента: search_memory поверх FTS5."""
        self._memory_root()  # проверка, что память вообще настроена

        async def action(db: AsyncSession) -> list[MemoryHitView]:
            hits = await memory_search(db, query, limit=limit)
            return [MemoryHitView(path=hit.path, snippet=hit.snippet) for hit in hits]

        return await self._read(action)

    # --- конфигурация и секреты (план 2026-07-27) --------------------------

    @staticmethod
    def _flat_keys(raw: dict[str, Any], prefix: str = "") -> set[str]:
        """Пути листьев mapping'а в форме `runtime.autonomy` — что он задаёт."""
        keys: set[str] = set()
        for key, value in raw.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                keys |= GatewayService._flat_keys(value, f"{path}.")
            else:
                keys.add(path)
        return keys

    @property
    def config_path(self) -> Path:
        """Проектный конфиг рядом с рабочей папкой."""
        return self.workspace / PROJECT_CONFIG_NAME

    @property
    def settings_path(self) -> Path:
        """Файл, который правит интерфейс (спека 2026-08-06).

        Настройки, провайдеры и MCP — про пользователя и его машину, а не про
        папку, поэтому пишутся в ~/.svarog/svarog.yaml и действуют во всех
        корнях. В режиме тенанта user_config_path не задан: там правки
        остаются в конфиге тенанта.
        """
        return self.user_config_path or self.config_path

    def _not_in_editable_file(self, name: str) -> str:
        """Провайдера нет ни в одном файле, который правит интерфейс."""
        if self.settings_path == self.config_path:
            # Глобальный слой не наш (режим тенанта): правка возможна только
            # в проектном файле, а провайдер пришёл из ~/.svarog.
            return (
                f"провайдер '{name}' описан не в проектном svarog.yaml "
                "(вероятно, в ~/.svarog/svarog.yaml) — правьте его там"
            )
        return (
            f"провайдер '{name}' не описан ни в {self.settings_path}, "
            f"ни в {self.config_path} — правьте файл, где он объявлен"
        )

    def _provider_file(self, name: str) -> Path:
        """Файл, где объявлен провайдер: правки должны идти именно в него.

        Форма пишет в глобальный слой, но провайдер мог быть объявлен в
        проектном. Удаление «не из того» файла прошло бы с нулём изменений, а
        merge продолжил бы отдавать провайдера — молчаливый холостой ход.
        """
        if name in (self._project_raw().get("models") or {}).get("providers", {}):
            return self.config_path
        return self.settings_path

    def _project_raw(self) -> dict[str, Any]:
        """Сырой проектный конфиг — чтобы знать, что он перекрывает."""
        if not self.config_path.is_file():
            return {}
        loaded = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}

    def _config_yaml(self) -> tuple[dict[str, Any], str]:
        """Сырой mapping файла и его текст (для диффа). Нет файла — пусто."""
        path = self.settings_path
        if not path.is_file():
            return {}, ""
        text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: верхний уровень должен быть mapping")
        return raw, text

    def describe_config(self) -> ConfigView:
        """Форма показывает то, что лежит в файле, а не снимок при старте.

        `self.cfg` зафиксирован в конструкторе и не перечитывается: если
        показывать его, после «Сохранить» форма откатит значение на глазах
        у человека, хотя на диске уже новое.
        """
        try:
            current = load_config(project_dir=self.workspace)
        except ConfigError:
            # Файл поломан снаружи — показываем снимок, с которым идут run'ы.
            current = self.cfg
        # Правим глобальный слой, а действует проектный поверх него: поля,
        # которые проект перекрывает, помечаем — иначе правка глобального
        # значения проходит успешно и не даёт никакого эффекта.
        overridden = (
            self._flat_keys(self._project_raw())
            if self.settings_path != self.config_path
            else set()
        )
        return describe_config(current, str(self.settings_path), overridden=overridden)

    def _validate_effective(self, project_raw: dict[str, Any]) -> None:
        """Проверить правку так, как её увидит load_config: merge поверх user-уровня.

        Проектный svarog.yaml имеет право быть частичным (§13): в воркспейсе
        без полного конфига он дополняет ~/.svarog/svarog.yaml. Валидировать
        его в одиночку нельзя — любая правка падала бы «Field required»
        (найдено 31.07.2026 на добавлении провайдера из настроек).
        """
        user_path = USER_CONFIG_PATH.expanduser()
        base: dict[str, Any] = {}
        if user_path.is_file() and user_path.resolve() != self.config_path.resolve():
            try:
                loaded = yaml.safe_load(user_path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                # Пользовательский файл сломали после старта serve: без него
                # merge не проверить — честный 422, а не 500 из недр yaml.
                raise ValueError(
                    f"не могу проверить правку: {user_path} не читается: {exc}"
                ) from exc
            if isinstance(loaded, dict):
                base = loaded
        SvarogConfig.model_validate(deep_merge(base, project_raw))

    def _validate_effective_user(self, user_raw: dict[str, Any]) -> None:
        """Проверить правку пользовательского слоя: проектное ложится поверх неё.

        Зеркало `_validate_effective`: там правится верхний слой, здесь нижний,
        и порядок merge обратный. Проверять правку в одиночку так же нельзя —
        частичный пользовательский файл валиден только вместе с проектным.
        """
        project: dict[str, Any] = {}
        if self.config_path.is_file():
            try:
                loaded = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise ValueError(
                    f"не могу проверить правку: {self.config_path} не читается: {exc}"
                ) from exc
            if isinstance(loaded, dict):
                project = loaded
        SvarogConfig.model_validate(deep_merge(user_raw, project))

    def _updated_config_text(self, values: dict[str, Any]) -> tuple[str, str]:
        """Текст файла до и после правки; заодно проверяет результат схемой."""
        raw, before = self._config_yaml()
        # apply_values проверяет, что путь вообще разрешён форме.
        merged = apply_values(raw, values)
        try:
            if self.settings_path == self.config_path:
                self._validate_effective(merged)
            else:
                self._validate_effective_user(merged)
        except ValidationError as exc:
            raise ConfigError(str(exc)) from exc
        # Правим текст построчно, а не пересобираем: иначе из файла пропадут
        # комментарии и пустые строки, которые человек ведёт руками.
        after = patch_yaml_text(before, values)
        # Патчер работает по строкам и не понимает flow-style (`{a: b}`) и
        # прочую экзотику. Проверять надо именно результат записи, а не
        # merged-словарь: иначе на диск ляжет файл, который больше не читается.
        try:
            written = yaml.safe_load(after) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"правка сломала бы {self.settings_path}: {exc}") from exc
        if written != merged:
            raise ConfigError(
                f"не удалось безопасно отредактировать {self.settings_path}: "
                "файл написан в форме, которую построчная правка не поддерживает. "
                "Отредактируйте его вручную."
            )
        return before, after

    def preview_config(self, values: dict[str, Any]) -> ConfigDiffView:
        """Что будет записано — без записи. Валидация та же, что при чтении."""
        before, after = self._updated_config_text(values)
        lines = diff_lines(before, after)
        return ConfigDiffView(
            path=str(self.settings_path),
            lines=lines,
            changes=sum(1 for line in lines if line.kind != "same"),
        )

    async def write_config(self, values: dict[str, Any]) -> ConfigDiffView:
        """Записать правку и, если ни один запуск не жив, перечитать конфиг.

        Конфиг под работающим run не меняется (ADR-0015 §0.4), поэтому при
        живом запуске снимок остаётся прежним, а ответ честно говорит, что
        правка вступит в силу позже.
        """
        view = self.preview_config(values)
        _, after = self._updated_config_text(values)
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(after, encoding="utf-8")
        # Глобальный слой общий: соседние корни держат свои снимки конфига.
        if self.settings_path != self.config_path and self.on_user_config_written is not None:
            await self.on_user_config_written()
        if not await self.reload_config():
            return view.model_copy(update={"restart_required": True})
        return view

    async def _write_deep(
        self,
        values: dict[str, Any],
        removes: Sequence[str] = (),
        target: Path | None = None,
    ) -> ConfigDiffView:
        """Записать вложенные правки svarog.yaml (провайдеры/MCP/executor-дефолты).

        Тот же контракт, что write_config: результат валидируется полной
        схемой ДО записи (кривая правка — ValueError → 422, файл не тронут),
        конфиг перечитывается только без живых runs.

        `target` — файл правки; None означает проектный. MCP пишутся в
        пользовательский слой, и проверять их надо в обратном порядке слияния:
        не «правка поверх пользовательского», а «проектное поверх правки».
        """
        path = target or self.settings_path
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        after = set_deep_values(before, values)
        for remove_path in removes:
            after = remove_deep_key(after, remove_path)
        parsed = yaml.safe_load(after) or {}
        try:
            if path == self.config_path:
                self._validate_effective(parsed)
            else:
                self._validate_effective_user(parsed)
        except ValidationError as exc:
            first = exc.errors()[0].get("msg", str(exc)) if exc.errors() else str(exc)
            raise ValueError(f"правка делает конфиг невалидным: {first}") from exc
        lines = diff_lines(before, after)
        view = ConfigDiffView(
            path=str(path),
            lines=lines,
            changes=sum(1 for line in lines if line.kind != "same"),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(after, encoding="utf-8")
        # Пользовательский слой общий для всех корней: соседние сервисы держат
        # свои снимки конфига и без уведомления не увидели бы правку до
        # перезапуска — «глобально» работало бы только там, где нажали кнопку.
        if path != self.config_path and self.on_user_config_written is not None:
            await self.on_user_config_written()
        if not await self.reload_config():
            return view.model_copy(update={"restart_required": True})
        return view

    async def reload_config(self) -> bool:
        """Перечитать конфиг с диска. False — есть живые runs, снимок оставлен.

        Живой run продолжает работать на своём снимке (ADR-0015): подменять
        конфиг под ним значило бы поменять правила посреди запуска.
        """
        if await self._any_run_live():
            return False
        self.cfg = load_config(project_dir=self.workspace)
        self._runner = TaskRunner(self.cfg, self.workspace, role=self.role)
        await self.close_warm_sessions()
        self._catalog.clear()
        self._catalog_failures.clear()
        return True

    async def _any_run_live(self) -> bool:
        async def action(db: AsyncSession) -> bool:
            found = await db.execute(select(Run).where(Run.state.in_(_LIVE_STATES)).limit(1))
            return found.scalar_one_or_none() is not None

        return await self._read(action)

    async def add_provider(
        self,
        name: str,
        base_url: str,
        model: str,
        api_key: str | None = None,
    ) -> ConfigDiffView:
        """Добавить/обновить провайдера в models.providers (настройки, 31.07.2026).

        Ключ не пишется в yaml никогда (ADR-0006): значение уходит в
        SecretStore под ref `<NAME>_API_KEY`, конфиг получает только ссылку.
        """
        if not re.fullmatch(r"[A-Za-z][\w-]{0,63}", name):
            raise ValueError(
                "имя провайдера — латиница/цифры/дефис/подчёркивание, начинается с буквы"
            )
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url должен начинаться с http(s)://")
        if not model.strip():
            raise ValueError("укажите модель провайдера")
        values: dict[str, Any] = {
            f"models.providers.{name}.base_url": base_url.strip(),
            f"models.providers.{name}.model": model.strip(),
        }
        if api_key:
            ref = re.sub(r"\W", "_", name.upper()) + "_API_KEY"
            secrets_path = Path(
                self.cfg.secrets.path or Path.home() / ".svarog" / "secrets.json"
            ).expanduser()
            FileSecretStore(secrets_path).set(ref, api_key)
            values[f"models.providers.{name}.api_key_ref"] = ref
        return await self._write_deep(values)

    async def rename_provider(self, name: str, new_name: str) -> ConfigDiffView:
        """Перенести запись models.providers под новое имя.

        api_key_ref переезжает как есть — секрет в SecretStore остаётся под
        прежним ref, ключ перевводить не нужно. models.default и models.auxiliary
        обновляются, если указывали на старое имя. exclude_defaults: в yaml
        переезжает только то, что человек реально задал, без шума дефолтных полей.
        """
        provider = self.cfg.models.providers.get(name)
        if provider is None:
            raise UnknownProviderError(f"провайдер '{name}' не описан в models.providers")
        if not re.fullmatch(r"[A-Za-z][\w-]{0,63}", new_name):
            raise ValueError(
                "имя провайдера — латиница/цифры/дефис/подчёркивание, начинается с буквы"
            )
        if new_name == name:
            raise ValueError("новое имя совпадает со старым")
        if new_name in self.cfg.models.providers:
            raise ValueError(f"провайдер '{new_name}' уже существует")
        file = self._provider_file(name)
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {} if file.exists() else {}
        models_raw = raw.get("models") or {}
        providers = models_raw.get("providers") or {}
        # Переименование = запись под новым именем плюс удаление старого; оба
        # действия обязаны идти в один файл, иначе останется дубликат.
        if name not in providers:
            raise ValueError(self._not_in_editable_file(name))
        dump = provider.model_dump(exclude_defaults=True)
        values: dict[str, Any] = {
            f"models.providers.{new_name}.{key}": value for key, value in dump.items()
        }
        if self.cfg.models.default == name:
            values["models.default"] = new_name
        if self.cfg.models.auxiliary == name:
            values["models.auxiliary"] = new_name
        return await self._write_deep(values, removes=[f"models.providers.{name}"], target=file)

    async def remove_provider(self, name: str) -> ConfigDiffView:
        """Удалить запись models.providers; дефолтного и вспомогательного — отказ.

        Удаляя последний ключ из models.providers, удаляют и саму обёртку,
        чтобы валидация не упала на None.
        """
        if name not in self.cfg.models.providers:
            raise UnknownProviderError(f"провайдер '{name}' не описан в models.providers")
        if name == self.cfg.models.default:
            raise ValueError(
                "нельзя удалить провайдера по умолчанию — сначала переключите «по умолчанию»"
            )
        if name == self.cfg.models.auxiliary:
            raise ValueError(
                "нельзя удалить провайдера, назначенного вспомогательным "
                "(models.auxiliary) — сначала переназначьте его"
            )
        file = self._provider_file(name)
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {} if file.exists() else {}
        models_raw = raw.get("models") or {}
        providers = models_raw.get("providers") or {}
        if name not in providers:
            raise ValueError(self._not_in_editable_file(name))
        # Пустая обёртка (`providers:` без ключей) парсится в None и валит
        # валидацию — удаляя последний ключ проектного файла, снимаем и её.
        if len(providers) <= 1:
            target = "models" if set(models_raw.keys()) <= {"providers"} else "models.providers"
        else:
            target = f"models.providers.{name}"
        return await self._write_deep({}, removes=[target], target=file)

    async def set_executor_defaults(
        self, executor: str, provider: str | None = None, model: str | None = None
    ) -> ConfigDiffView:
        """Дефолты модели/провайдера per-executor (настройки, 31.07.2026).

        native: провайдер → models.default, модель → модель этого провайдера.
        opencode/codex: провайдер → base_url/api_key_ref секции external (без
        /v1 — адаптер добавит сам), модель → executor.external.model.
        claude-code: только модель (провайдер — его подписка).
        """
        values: dict[str, Any] = {}
        if executor == "native":
            target = provider or self.cfg.models.default
            if target not in self.cfg.models.providers:
                raise ValueError(f"провайдер '{target}' не найден в models.providers")
            if provider is not None:
                values["models.default"] = provider
            if model is not None:
                values[f"models.providers.{target}.model"] = model
        elif executor in EXTERNAL_ADAPTERS:
            if self.cfg.executor.external is None:
                raise ValueError(
                    "секции executor.external нет в svarog.yaml — добавьте её "
                    "(svarog init пишет заготовку)"
                )
            if provider is not None:
                if executor == "claude-code":
                    raise ValueError("у claude-code свой провайдер (подписка)")
                card = self.cfg.models.providers.get(provider)
                if card is None:
                    raise ValueError(f"провайдер '{provider}' не найден в models.providers")
                base = card.base_url.rstrip("/").removesuffix("/v1")
                values["executor.external.base_url"] = base
                values["executor.external.auth"] = "api-key"
                if card.api_key_ref:
                    values["executor.external.api_key_ref"] = card.api_key_ref
            if model is not None:
                values["executor.external.model"] = model
        else:
            raise ValueError(f"неизвестный executor '{executor}'")
        if not values:
            raise ValueError("нечего менять: укажите provider и/или model")
        return await self._write_deep(values)

    def _project_mcp_names(self) -> set[str]:
        """Имена серверов, объявленных именно в проектном файле.

        Действующий набор берём из `self.cfg` — там валидация и умолчания, — а
        происхождение приходится смотреть в сыром файле: после merge слои уже
        неразличимы.
        """
        servers = (self._project_raw().get("mcp") or {}).get("servers")
        return set(servers) if isinstance(servers, dict) else set()

    def list_mcp(self) -> list[McpServerView]:
        """Действующие MCP-серверы с указанием, где лежит каждый (вкладка MCP).

        Показываем и глобальные, и проектные: в запуск попадают оба слоя, и
        список только из глобальных врал бы про то, что видит агент.
        """
        project_names = self._project_mcp_names()
        return [
            McpServerView(
                name=name,
                command=server.command,
                args=list(server.args),
                env_refs=list(server.env_refs),
                risk=server.risk,
                scope="project" if name in project_names else "user",
            )
            for name, server in self.cfg.mcp.servers.items()
        ]

    async def test_mcp(
        self, command: str, args: Sequence[str], env_refs: Sequence[str]
    ) -> McpTestView:
        """Реально подключиться к MCP-серверу и сделать discovery (проверка)."""
        server = MCPServerConfig(command=command, args=list(args), env_refs=list(env_refs))
        probe = MCPConfig(servers={"probe": server})
        backends: list[MCPBackend] = []
        try:
            async with asyncio.timeout(20):
                backends = await connect_mcp_servers(probe, self._runner._host_store)
                tools = [spec.name for backend in backends for spec in backend.specs()]
            return McpTestView(ok=True, tools=tools)
        except Exception as exc:
            return McpTestView(ok=False, error=str(exc) or type(exc).__name__)
        finally:
            for backend in backends:
                with contextlib.suppress(Exception):
                    await backend.close()

    async def add_mcp(
        self,
        name: str,
        command: str,
        args: Sequence[str],
        env_refs: Sequence[str],
        risk: str,
    ) -> ConfigDiffView:
        if not re.fullmatch(r"[A-Za-z][\w-]{0,63}", name):
            raise ValueError("имя сервера — латиница/цифры/дефис/подчёркивание, начинается с буквы")
        if not command.strip():
            raise ValueError("укажите команду запуска MCP-сервера")
        values: dict[str, Any] = {
            f"mcp.servers.{name}.command": command.strip(),
            f"mcp.servers.{name}.args": list(args),
            f"mcp.servers.{name}.risk": risk,
        }
        if env_refs:
            values[f"mcp.servers.{name}.env_refs"] = list(env_refs)
        # MCP подключается к самому Сварогу: новые серверы уходят в
        # пользовательский слой и работают во всех корнях. В режиме тенанта
        # user_config_path не задан — там запись остаётся в конфиге тенанта.
        return await self._write_deep(values)

    async def remove_mcp(self, name: str) -> ConfigDiffView:
        if name not in self.cfg.mcp.servers:
            raise ValueError(f"MCP-сервер '{name}' не найден в конфиге")
        # Правим тот файл, где запись лежит: удалять проектный сервер из
        # пользовательского файла (и наоборот) значило бы молча ничего не
        # сделать — merge продолжил бы отдавать его агенту.
        file = self.config_path if name in self._project_mcp_names() else self.user_config_path
        if file is None:
            file = self.config_path
        # Пустая обёртка (`servers:` без ключей) парсится в None и валит
        # валидацию — удаляя последний сервер, снимаем и обёртку.
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {} if file.exists() else {}
        mcp_raw = raw.get("mcp") or {}
        servers = mcp_raw.get("servers") or {}
        if len(servers) <= 1:
            key = "mcp" if set(mcp_raw.keys()) <= {"servers"} else "mcp.servers"
        else:
            key = f"mcp.servers.{name}"
        return await self._write_deep({}, removes=[key], target=file)

    def list_secrets(self) -> list[SecretView]:
        """Имена секретов и найдено ли значение. Значения не возвращаются никогда."""
        store = self._runner.store
        return [
            SecretView(name=name, present=bool(store.get(name))) for name in sorted(store.names())
        ]

    # --- named workspaces и артефакты (ADR-0017) ---------------------------

    async def create_workspace(self, name: str) -> WorkspaceView:
        path = create_named_workspace(
            self.workspace, name, limit=self.cfg.cloud.max_named_workspaces
        )
        return WorkspaceView(name=name, size_bytes=0, modified_at=_mtime(path), busy=False)

    async def list_workspaces(self) -> list[WorkspaceView]:
        views = []
        for info in list_named_workspaces(self.workspace):
            views.append(
                WorkspaceView(
                    name=info.name,
                    size_bytes=info.size_bytes,
                    modified_at=info.modified_at,
                    busy=await self._workspace_busy(info.path.resolve()),
                )
            )
        return views

    async def delete_workspace(self, name: str) -> None:
        path = resolve_named_workspace(self.workspace, name).resolve()
        if await self._workspace_busy(path):
            raise WorkspaceBusyError(f"workspace '{name}' занят активным run — удаление отклонено")
        # Тёплые sandbox'ы сессий этого workspace закрываем до rmtree —
        # иначе контейнер останется с mount'ом удалённого дерева.
        for session_id, slot in list(self._warm.items()):
            if slot.workspace == path:
                await self._drop_warm(session_id)
        delete_named_workspace(self.workspace, name)

    def workspace_target(self, name: str, relative: str) -> Path:
        """Файл/каталог внутри named workspace (confinement — в provision)."""
        return resolve_workspace_file(self.workspace, name, relative)

    def archive_workspace(self, name: str) -> Path:
        """tar.gz снапшот named workspace во временном файле (вызывающий удаляет)."""
        base = resolve_named_workspace(self.workspace, name)
        fd, tmp = tempfile.mkstemp(prefix=f"svarog-ws-{name}-", suffix=".tar.gz")
        os.close(fd)
        with tarfile.open(tmp, "w:gz") as tar:
            # tarfile не следует symlink'ам (кладёт их как symlink-записи) —
            # содержимое за пределами workspace в архив не утекает.
            tar.add(base, arcname=name)
        return Path(tmp)

    async def run_diff(self, run_id: str) -> RunDiffView:
        """Диф run'а: патч его step-коммитов (Run-Id trailer, Flow C) +
        незакоммиченные изменения рабочего дерева (ADR-0017 §2)."""

        async def action(db: AsyncSession) -> Run:
            return await find_run_by_prefix(db, run_id)

        run = await self._read(action)
        workspace = Path(run.workspace) if run.workspace else self.workspace
        committed = uncommitted = ""
        repo = GitRepo(workspace)
        if (
            workspace.is_dir()
            and await self._workspace_owns_repo(repo)
            and await repo.has_commits()
        ):
            _, uncommitted, _ = await repo._git("diff", "HEAD", check=False)
            shas = [sha for sha, rid in await repo.log_with_run_ids() if run.id in rid.split(",")]
            if shas:
                newest, oldest = shas[0], shas[-1]
                code, base, _ = await repo._git("rev-parse", f"{oldest}^", check=False)
                base_ref = base.strip() if code == 0 else _EMPTY_TREE
                _, committed, _ = await repo._git("diff", base_ref, newest, check=False)
        return RunDiffView(run_id=run.id, committed=committed, uncommitted=uncommitted)

    @staticmethod
    async def _workspace_owns_repo(repo: GitRepo) -> bool:
        """Диф считается только по репо, чей корень — сам workspace (та же
        граница, что у Flow C): named workspace внутри git-корня сервиса не
        должен показывать родительский репозиторий (ADR-0017)."""
        if not await repo.is_repo():
            return False
        top = await repo.toplevel()
        return top is not None and top.resolve() == repo.path.expanduser().resolve()

    async def sweep_workspaces(self) -> list[Path]:
        """Retention-GC терминальных task-workspace'ов (named не трогает)."""
        days = self.cfg.cloud.workspace_retention_days
        if days <= 0:
            return []

        async def action(db: AsyncSession) -> set[str]:
            result = await db.execute(
                select(Run.workspace).where(Run.state.in_(_LIVE_STATES), Run.workspace.is_not(None))
            )
            return {ws for (ws,) in result.all() if ws}

        active = await self._read(action)
        return sweep_task_workspaces(self.workspace, retention_days=days, active=active)

    async def _maybe_sweep_workspaces(self) -> None:
        if self.cfg.cloud.workspace_retention_days <= 0:
            return
        now = time.monotonic()
        if self._last_gc and now - self._last_gc < _GC_INTERVAL_SEC:
            return
        self._last_gc = now
        with contextlib.suppress(Exception):
            await self.sweep_workspaces()

    # --- супервизор refuel (§6.10, ADR-0005) ------------------------------

    async def supervise_once(self) -> list[str]:
        """Один проход: поднять refuel-suspended runs. Возвращает run_id'ы, для
        которых запущено авто-возобновление (для тестов и наблюдаемости)."""
        await self._maybe_sweep_workspaces()  # retention-GC task-workspaces (ADR-0017)
        with contextlib.suppress(Exception):
            await self._sweep_warm_sessions()  # idle-GC тёплых sandbox'ов (ADR-0017)
        sup = self.cfg.supervisor
        if not sup.auto_resume_refuel:
            return []
        if self.cfg.runtime.max_refuel_rounds:
            # Блок B: механизм продолжения выбирается конфигом, а не сосуществует.
            # При включённом автопродолжении run продолжает себя внутри цикла, а
            # refuel-приостановка означает исчерпанный потолок — намеренную
            # остановку, решение по которой принимает человек. Поднимать её
            # супервизором значило бы отменять потолок: ручной resume выдаёт
            # новый бюджет раундов, и получилась бы петля.
            return []

        async def fetch(db: AsyncSession) -> list[Run]:
            return await TraceRecorder(db).find_refuel_suspended_runs()

        resumed: list[str] = []
        for run in await self._read(fetch):
            if run.id in self._inflight:
                continue  # авто-resume уже в полёте — не дублируем
            if self._auto_resumes.get(run.id, 0) >= sup.max_auto_resumes:
                continue  # предохранитель от петли исчерпан
            self._auto_resumes[run.id] = self._auto_resumes.get(run.id, 0) + 1
            self._spawn_supervised_resume(run.id)
            resumed.append(run.id)
        return resumed

    def _spawn_supervised_resume(self, run_id: str) -> None:
        self._inflight.add(run_id)
        self.events.reset(run_id)

        async def wrapped() -> None:
            try:
                await self._resume_bg(run_id)
            finally:
                self._inflight.discard(run_id)

        self._spawn(wrapped())

    async def run_supervisor(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        """Периодически поднимать refuel-suspended runs (§6.10).

        Живёт в долгоживущих процессах (serve/telegram); останавливается по
        should_stop или отмене задачи (lifespan/сигнал). Ошибка прохода не рвёт
        цикл. Естественный потолок числа возобновлений — max_iterations run'а,
        поверх него — supervisor.max_auto_resumes.
        """
        interval = self.cfg.supervisor.interval_sec
        while should_stop is None or not should_stop():
            with contextlib.suppress(Exception):
                await self.supervise_once()
            await asyncio.sleep(interval)

    def list_skills(self) -> list[SkillCard]:
        scan = scan_skills(skills_dirs(self.cfg, self.workspace))
        return [
            SkillCard(
                name=s.name,
                description=s.metadata.description,
                version=s.metadata.version,
                risk=s.metadata.risk.value,
            )
            for s in scan.skills
        ]


def _mtime(path: Path) -> "datetime":
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


def _summary(run: Run) -> RunSummary:
    return RunSummary(
        run_id=run.id,
        state=run.state.value,
        task=run.task,
        autonomy=run.autonomy,
        iterations=run.iterations,
        tokens_used=run.tokens_used,
        cost_usd=run.cost_usd,
        error=run.error,
    )


__all__ = [
    "ApprovalNotFoundError",
    "GatewayService",
    "RunNotFoundError",
]
