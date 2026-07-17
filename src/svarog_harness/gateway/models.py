"""Pydantic-схемы REST/WebSocket API (§10.4, cloud-режим — ADR-0017)."""

from datetime import datetime
from typing import Any

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

    @model_validator(mode="after")
    def _one_workspace_source(self) -> "CreateRunRequest":
        if self.repo is not None and self.workspace is not None:
            raise ValueError("repo и workspace взаимоисключающие: задайте один источник")
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

    @model_validator(mode="after")
    def _one_workspace_source(self) -> "CreateSessionRequest":
        if self.repo is not None and self.workspace is not None:
            raise ValueError("repo и workspace взаимоисключающие: задайте один источник")
        return self


class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1)
    autonomy: AutonomyMode | None = None


class SessionView(BaseModel):
    session_id: str
    title: str
    workspace: str | None = None
    runs: list[RunSummary]
