"""Pydantic-схемы REST/WebSocket API (§10.4, cloud-режим — ADR-0017)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from svarog_harness.config.schema import AutonomyMode


class RepoSpec(BaseModel):
    """Git-источник одноразового task-workspace (ADR-0017 §1)."""

    url: str = Field(min_length=1, description="https:// или ssh URL репозитория")
    ref: str | None = Field(default=None, description="Ветка/тег; None — default branch")
    # Имя секрета с credentials в tenant-store; None — конвенциональный
    # "git.credentials" (отсутствие секрета = анонимный clone).
    credentials_ref: str | None = None


class CreateRunRequest(BaseModel):
    task: str = Field(min_length=1, description="Задача для агента")
    # None — взять режим из конфигурации; иначе переопределить для этого run.
    autonomy: AutonomyMode | None = None
    # Источник workspace (ADR-0017): git-клон в одноразовый task-workspace
    # ЛИБО постоянный named workspace тенанта; оба None — workspace сервиса.
    repo: RepoSpec | None = None
    workspace: str | None = Field(default=None, description="Имя named workspace")
    # Абсолютный путь папки-корня (single-tenant, спека 2026-07-30);
    # взаимоисключающ с repo и workspace — это третий источник workspace.
    path: str | None = None

    @model_validator(mode="after")
    def _one_workspace_source(self) -> "CreateRunRequest":
        if self.repo is not None and self.workspace is not None:
            raise ValueError("repo и workspace взаимоисключающие: задайте один источник")
        if self.path is not None and (self.repo is not None or self.workspace is not None):
            raise ValueError("path взаимоисключающ с repo и workspace: задайте один источник")
        return self


class RunRef(BaseModel):
    run_id: str
    state: str


class RunSummary(BaseModel):
    run_id: str
    state: str
    task: str
    autonomy: str
    iterations: int
    tokens_used: int
    cost_usd: float
    error: str | None = None


class ToolCallView(BaseModel):
    tool_name: str
    risk_level: str | None
    policy_decision: str | None
    status: str
    error: str | None = None


class RunDetail(RunSummary):
    messages: list[dict[str, Any]]
    tool_calls: list[ToolCallView]
    checks: list[dict[str, Any]]


class SkillCard(BaseModel):
    name: str
    description: str
    version: str
    risk: str


class ApprovalView(BaseModel):
    approval_id: str
    run_id: str
    action_type: str
    payload: dict[str, Any]


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    reason: str | None = None


class AnswerRequest(BaseModel):
    # Ответ человека на вопрос ask_user; пусто — продолжить без ответа (§6.5).
    answer: str = ""


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="Слаг [a-z0-9-]")


class WorkspaceView(BaseModel):
    name: str
    size_bytes: int
    modified_at: datetime
    busy: bool  # есть живой run в этом workspace (lease, ADR-0015 §0.5)


class FsEntryView(BaseModel):
    """Подкаталог из GET /fs (пикер рабочей папки, спека 2026-07-30)."""

    name: str
    path: str
    # Нечитаемый каталог показываем, но выбрать не даём (PermissionError).
    accessible: bool = True


class FsListingView(BaseModel):
    path: str
    parent: str | None  # None — корень ФС, выше некуда
    entries: list[FsEntryView]


class RecentRootView(BaseModel):
    """Недавний корень из реестра; exists=False рисуется приглушённым."""

    path: str
    exists: bool
    last_used: datetime


class RootInspectView(BaseModel):
    """Проверка папки-кандидата до создания чата (ADR-0015 §0.3 + ADR-0018).

    blocking=True — без явного согласия run в этой папке будет отклонён
    (control-plane пересекается с workspace, sandbox не local-trusted):
    пикер показывает диалог «принять риски». blocking=False с непустыми
    warnings — режим local-trusted, где пересечение — документированный
    trade-off и не блокирует.
    """

    path: str
    overlap_warnings: list[str]
    blocking: bool


class FileEntry(BaseModel):
    name: str
    is_dir: bool
    size_bytes: int


class DirListing(BaseModel):
    path: str
    entries: list[FileEntry]


class RunDiffView(BaseModel):
    run_id: str
    # Патч коммитов run'а (по Run-Id trailer, Flow C) и незакоммиченные
    # изменения рабочего дерева; пустые строки — нет git/изменений.
    committed: str
    uncommitted: str


class CancelView(BaseModel):
    run_id: str
    # "cancelled" — терминализирован сразу (не было живой ноги);
    # "cancelling" — флаг поставлен, loop завершит run на границе итерации.
    state: str


class WhoamiView(BaseModel):
    tenant_id: str
    role: str
    active_runs: int
    total_cost_usd: float
    total_tokens: int


class CreateSessionRequest(BaseModel):
    """Сессия gateway-chat (ADR-0017 §2): workspace фиксируется на всю серию."""

    title: str = Field(default="", max_length=200)
    repo: RepoSpec | None = None
    workspace: str | None = None
    # Абсолютный путь папки-корня (single-tenant, спека 2026-07-30);
    # взаимоисключающ с repo и workspace — это третий источник workspace.
    path: str | None = None
    # Человек явно принял пересечение workspace с control-plane (ADR-0018):
    # runs сессии пойдут с allow_layout_overlap. Только single-tenant.
    accept_overlap: bool = False

    @model_validator(mode="after")
    def _one_workspace_source(self) -> "CreateSessionRequest":
        if self.repo is not None and self.workspace is not None:
            raise ValueError("repo и workspace взаимоисключающие: задайте один источник")
        if self.path is not None and (self.repo is not None or self.workspace is not None):
            raise ValueError("path взаимоисключающ с repo и workspace: задайте один источник")
        return self


class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1)
    autonomy: AutonomyMode | None = None
    # Выбор в поле ввода — свойство сообщения, а не правка svarog.yaml.
    # None во всех трёх — поведение по конфигу.
    executor: Literal["native", "external"] | None = None
    provider: str | None = None
    model: str | None = None
    # Адаптер внешнего агента — тоже выбор в поле ввода, а не правка
    # svarog.yaml; None — взять адаптер из конфига (см. RunOverride.adapter).
    adapter: Literal["claude-code", "codex", "opencode"] | None = None
    # Относительные пути из `.attachments/` этой сессии (задача 7).
    attachments: list[str] = []


class SessionView(BaseModel):
    session_id: str
    title: str
    workspace: str | None = None
    runs: list[RunSummary]


class MemoryPageView(BaseModel):
    """Страница памяти в дереве: путь относительно memory/ и размер."""

    path: str
    size_bytes: int
    modified_at: datetime


class MemoryHitView(BaseModel):
    path: str
    snippet: str


class MemoryFileView(BaseModel):
    path: str
    text: str
    size_bytes: int
    modified_at: datetime


class ProviderView(BaseModel):
    """Запись models.providers для селектора модели, без api_key_ref (ADR-0006)."""

    name: str
    base_url: str
    model: str
    is_default: bool


class ModelCardView(BaseModel):
    """Модель из каталога провайдера — то же, что ModelCard, но для ответа API."""

    id: str
    name: str | None = None
    context_length: int | None = None
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None


class SecretView(BaseModel):
    """Секрет для экрана настроек: имя и факт наличия, без значения (ADR-0006)."""

    name: str
    present: bool


class ExecutorOptionView(BaseModel):
    """Вариант исполнителя для селекта поля ввода (задача 3)."""

    value: str
    kind: Literal["native", "external"]
    adapter: str | None = None
    available: bool
    is_active: bool


class ThreadItemView(BaseModel):
    """Элемент ленты в той же форме, в какой его собирает живой поток.

    Один тип на реплику, речь агента и вызов инструмента: клиент рисует
    историю и живые события одним компонентом, иначе они разойдутся.
    """

    kind: Literal["user", "say", "call"]
    text: str = ""
    server: str | None = None  # имя MCP-сервера; None — свой инструмент
    name: str = ""
    arg: str = ""
    result: str = ""
    status: str = ""


class SessionThread(BaseModel):
    session_id: str
    title: str
    items: list[ThreadItemView]


class SessionSummary(BaseModel):
    """Строка списка сессий для навигатора веб-клиента.

    Отдельно от SessionView: там полный список runs, здесь только то, что
    нужно левому столбцу, — иначе навигатор тянет весь трейс всех сессий.
    """

    session_id: str
    title: str
    workspace: str | None = None
    # Корень сервиса, которому принадлежит сессия (спека 2026-07-30, финальное
    # ревью): для path-сессий — выбранный корень, для repo/named — default.
    # Отдельно от workspace (clone/task-каталог) — они расходятся для repo/named.
    # None — сессии, созданные до этого поля (миграции нет).
    root: str | None = None
    updated_at: datetime
    runs_count: int
    last_state: str | None = None


class SlashCommandView(BaseModel):
    """Слэш-команда веб-чата для автодополнения и справки."""

    name: str
    usage: str
    help: str


class FileSuggestionView(BaseModel):
    """Подсказка `@file` для автодополнения веб-чата."""

    path: str
    label: str


class AttachmentView(BaseModel):
    """Ответ на загрузку вложения (задача 7)."""

    path: str
    name: str
    size_bytes: int
    mime: str | None = None
    too_large_for_vision: bool = False
