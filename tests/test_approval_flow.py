"""Тесты approval + notify flow (#13, ADR-0005/0010): waiting_approval,
решение человека, resume с потреблением решения, request_approval tool."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.schema import AutonomyMode, PoliciesConfig, RuntimeConfig
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.policy.engine import PolicyEngine
from svarog_harness.runtime.checkpoint import LoopState
from svarog_harness.runtime.loop import AgentLoop
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import (
    Approval,
    ApprovalStatus,
    RunState,
    ToolCall,
    ToolCallStatus,
)
from svarog_harness.tools.approval import RequestApprovalTool
from svarog_harness.tools.base import RiskLevel, Tool, ToolResult
from svarog_harness.tools.registry import ToolRegistry
from svarog_harness.trace.lookup import ApprovalNotFoundError
from svarog_harness.trace.recorder import TraceRecorder


class _NoArgs(BaseModel):
    pass


class DeployTool(Tool[_NoArgs]):
    name = "deploy_preview"
    action_type = "deploy.preview"
    description = "тестовый high-risk tool"
    risk_level = RiskLevel.HIGH
    args_model = _NoArgs

    def __init__(self) -> None:
        self.executions = 0

    async def execute(self, args: _NoArgs) -> ToolResult:
        self.executions += 1
        return ToolResult.success("задеплоено")


class _TargetArgs(BaseModel):
    target: str


class DeployTargetTool(Tool[_TargetArgs]):
    """High-risk tool с параметром — на нём проверяется ремонт формы вызова
    (двойная сериализация/обёртка `{"arguments": {...}}`) на approval-пути."""

    name = "deploy_target"
    action_type = "deploy.target"
    description = "тестовый high-risk tool с параметром target"
    risk_level = RiskLevel.HIGH
    args_model = _TargetArgs

    def __init__(self) -> None:
        self.executions = 0

    async def execute(self, args: _TargetArgs) -> ToolResult:
        self.executions += 1
        return ToolResult.success(f"задеплоено в {args.target}")


class ScriptedProvider(ModelProvider):
    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)
        self.seen_messages: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.seen_messages.append(list(messages))
        return self.turns.pop(0)


def _tool_turn(*calls: ToolCallRequest) -> CompletionResult:
    return CompletionResult(
        content="", tool_calls=calls, usage=Usage(10, 5), finish_reason="tool_calls"
    )


def _final(text: str) -> CompletionResult:
    return CompletionResult(content=text, usage=Usage(10, 5), finish_reason="stop")


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


def _loop(
    provider: ModelProvider,
    db: AsyncSession,
    workspace: Path,
    tools: list[Tool[BaseModel]],
    *,
    autonomy: AutonomyMode = AutonomyMode.SUPERVISED,
) -> AgentLoop:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return AgentLoop(
        provider,
        registry,
        TraceRecorder(db),
        RuntimeConfig(),
        PolicyEngine(autonomy=autonomy, policies=PoliciesConfig(), workspace=workspace),
        workspace,
        model_name="test-model",
    )


async def _resume(loop: AgentLoop, db: AsyncSession, run_id: str) -> object:
    recorder = TraceRecorder(db)
    run, raw_state = await recorder.load_resumable(run_id)
    return await loop.resume(run, LoopState.from_dict(raw_state))


async def test_approved_call_executes_on_resume(db: AsyncSession, tmp_path: Path) -> None:
    deploy = DeployTool()
    provider = ScriptedProvider(
        [
            _tool_turn(ToolCallRequest(id="c1", name="deploy_preview", arguments_json="{}")),
            _final("готово"),
        ]
    )
    loop = _loop(provider, db, tmp_path, [deploy])
    outcome = await loop.run("задеплой", AutonomyMode.SUPERVISED)
    assert outcome.state is RunState.WAITING_APPROVAL  # type: ignore[union-attr]
    assert deploy.executions == 0  # ничего не исполнено до решения

    recorder = TraceRecorder(db)
    approval = (await db.execute(select(Approval))).scalar_one()
    await recorder.decide_approval(approval, approved=True, decided_by="test", reason="ок")

    resumed = await _resume(loop, db, outcome.run_id)
    assert resumed.state is RunState.COMPLETED  # type: ignore[attr-defined]
    assert deploy.executions == 1
    call = (await db.execute(select(ToolCall))).scalar_one()
    assert call.status is ToolCallStatus.SUCCEEDED
    assert call.policy_decision == "require_approval"


async def test_denied_call_reports_reason_to_model(db: AsyncSession, tmp_path: Path) -> None:
    deploy = DeployTool()
    provider = ScriptedProvider(
        [
            _tool_turn(ToolCallRequest(id="c1", name="deploy_preview", arguments_json="{}")),
            _final("понял, не деплою"),
        ]
    )
    loop = _loop(provider, db, tmp_path, [deploy])
    outcome = await loop.run("задеплой", AutonomyMode.SUPERVISED)
    assert outcome.state is RunState.WAITING_APPROVAL

    recorder = TraceRecorder(db)
    approval = (await db.execute(select(Approval))).scalar_one()
    await recorder.decide_approval(approval, approved=False, decided_by="test", reason="не сейчас")

    resumed = await _resume(loop, db, outcome.run_id)
    assert resumed.state is RunState.COMPLETED  # type: ignore[attr-defined]
    assert deploy.executions == 0
    # Модель получила фактическую причину отказа.
    last_request = provider.seen_messages[-1]
    assert "отклонен пользователем" in last_request[-1].content
    assert "не сейчас" in last_request[-1].content
    call = (await db.execute(select(ToolCall))).scalar_one()
    assert call.status is ToolCallStatus.DENIED


async def test_denied_call_with_repaired_arguments_shows_repair_in_trace(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Блок A §4 + §1 (approval): ремонт формы вызова должен остаться видимым
    в trace, даже когда вызов уходит в approval и получает отказ — иначе при
    связке «ремонт + отказ approval» trace не показывает, что прислала модель."""
    deploy = DeployTargetTool()
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(
                    id="c1",
                    name="deploy_target",
                    arguments_json='{"arguments": {"target": "prod"}}',
                )
            ),
            _final("понял, не деплою"),
        ]
    )
    loop = _loop(provider, db, tmp_path, [deploy])
    outcome = await loop.run("задеплой в prod", AutonomyMode.SUPERVISED)
    assert outcome.state is RunState.WAITING_APPROVAL

    recorder = TraceRecorder(db)
    approval = (await db.execute(select(Approval))).scalar_one()
    await recorder.decide_approval(approval, approved=False, decided_by="test", reason="не сейчас")

    resumed = await _resume(loop, db, outcome.run_id)
    assert resumed.state is RunState.COMPLETED  # type: ignore[attr-defined]
    assert deploy.executions == 0

    call = (await db.execute(select(ToolCall))).scalar_one()
    assert call.status is ToolCallStatus.DENIED
    # Починенная форма исполнена бы с target=prod — это видно в trace...
    assert call.arguments["target"] == "prod"
    # ...вместе со сведениями о самом ремонте и исходной строкой аргументов.
    assert call.arguments["_repairs"] == ["unwrapped"]
    assert "arguments" in call.arguments["_raw"]


async def test_pending_approval_keeps_waiting_without_duplicates(
    db: AsyncSession, tmp_path: Path
) -> None:
    deploy = DeployTool()
    provider = ScriptedProvider(
        [_tool_turn(ToolCallRequest(id="c1", name="deploy_preview", arguments_json="{}"))]
    )
    loop = _loop(provider, db, tmp_path, [deploy])
    outcome = await loop.run("задеплой", AutonomyMode.SUPERVISED)
    assert outcome.state is RunState.WAITING_APPROVAL

    # Resume без решения — снова waiting_approval, второй Approval не создается.
    resumed = await _resume(loop, db, outcome.run_id)
    assert resumed.state is RunState.WAITING_APPROVAL  # type: ignore[attr-defined]
    approvals = (await db.execute(select(Approval))).scalars().all()
    assert len(approvals) == 1


async def test_request_approval_tool_in_yolo(db: AsyncSession, tmp_path: Path) -> None:
    """Агент сам запрашивает подтверждение — critical даже в yolo (ADR-0010)."""
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(
                    id="c1",
                    name="request_approval",
                    arguments_json='{"action": "удалить прод-БД", "details": "DROP DATABASE x"}',
                )
            ),
            _final("получил добро"),
        ]
    )
    loop = _loop(provider, db, tmp_path, [RequestApprovalTool()], autonomy=AutonomyMode.YOLO)
    outcome = await loop.run("рискованная задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.WAITING_APPROVAL

    approval = (await db.execute(select(Approval))).scalar_one()
    assert approval.action_type == "approval.request"
    # Человеку показывают фактические детали (§12).
    assert approval.payload["arguments"]["details"] == "DROP DATABASE x"

    recorder = TraceRecorder(db)
    await recorder.decide_approval(approval, approved=True, decided_by="test")
    resumed = await _resume(loop, db, outcome.run_id)
    assert resumed.state is RunState.COMPLETED  # type: ignore[attr-defined]
    last_request = provider.seen_messages[-1]
    assert "пользователь одобрил" in last_request[-1].content


async def test_recorder_approval_helpers(db: AsyncSession) -> None:
    recorder = TraceRecorder(db)
    run = await recorder.start_run(task="t", autonomy="yolo", model="m")
    approval = await recorder.create_approval(
        run, action_type="approval.request", payload={"call_id": "c1"}
    )

    pending = await recorder.fetch_pending_approvals()
    assert [a.id for a in pending] == [approval.id]

    found = await recorder.find_approval_by_prefix(approval.id[:8])
    assert found.id == approval.id

    await recorder.decide_approval(found, approved=True, decided_by="cli")
    assert found.status is ApprovalStatus.APPROVED
    assert found.decided_at is not None
    assert await recorder.fetch_pending_approvals() == []

    with pytest.raises(ApprovalNotFoundError):
        await recorder.find_approval_by_prefix("нет-такого")


async def test_denied_call_explains_boundary_to_model(db: AsyncSession, tmp_path: Path) -> None:
    """Блок E §3: отказ в approval объясняет, что повторять запрос не нужно.

    В trace остаётся чистая причина отказа — текст-инструкция туда не уходит.
    """
    deploy = DeployTool()
    provider = ScriptedProvider(
        [
            _tool_turn(ToolCallRequest(id="c1", name="deploy_preview", arguments_json="{}")),
            _final("понял, не деплою"),
        ]
    )
    loop = _loop(provider, db, tmp_path, [deploy])
    outcome = await loop.run("задеплой", AutonomyMode.SUPERVISED)
    assert outcome.state is RunState.WAITING_APPROVAL

    recorder = TraceRecorder(db)
    approval = (await db.execute(select(Approval))).scalar_one()
    await recorder.decide_approval(approval, approved=False, decided_by="test", reason="не сейчас")

    resumed = await _resume(loop, db, outcome.run_id)
    assert resumed.state is RunState.COMPLETED  # type: ignore[attr-defined]

    # Модель получила и причину, и подсказку.
    model_text = provider.seen_messages[-1][-1].content
    assert "не сейчас" in model_text
    assert "повтор" in model_text.lower()

    # В trace — только причина.
    call = (await db.execute(select(ToolCall))).scalar_one()
    assert call.error is not None
    assert "не сейчас" in call.error
    assert "повтор" not in call.error.lower()
