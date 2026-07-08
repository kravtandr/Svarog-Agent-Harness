"""Pydantic-схемы REST/WebSocket API (§10.4)."""

from typing import Any

from pydantic import BaseModel, Field

from svarog_harness.config.schema import AutonomyMode


class CreateRunRequest(BaseModel):
    task: str = Field(min_length=1, description="Задача для агента")
    # None — взять режим из конфигурации; иначе переопределить для этого run.
    autonomy: AutonomyMode | None = None


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
