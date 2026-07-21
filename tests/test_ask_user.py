"""Тесты ask_user (§6.5): пауза на вопрос, текстовый ответ, таймаут → best-guess."""

from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path

import pytest
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
from svarog_harness.runtime.loop import AgentLoop, RunOutcome
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import Approval, ApprovalStatus, RunState, ToolCall, utcnow
from svarog_harness.tools.registry import ToolRegistry
from svarog_harness.tools.user_tools import AskUserTool
from svarog_harness.trace.recorder import TraceRecorder


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


def _ask_turn(question: str = "какой цвет?") -> CompletionResult:
    args = f'{{"question": "{question}"}}'
    return CompletionResult(
        content="",
        tool_calls=(ToolCallRequest(id="q1", name="ask_user", arguments_json=args),),
        usage=Usage(10, 5),
        finish_reason="tool_calls",
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


def _loop(provider: ModelProvider, db: AsyncSession, workspace: Path) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(AskUserTool())
    return AgentLoop(
        provider,
        registry,
        TraceRecorder(db),
        RuntimeConfig(),
        # yolo: ask_user всё равно приостанавливает — вопрос требует человека.
        PolicyEngine(autonomy=AutonomyMode.YOLO, policies=PoliciesConfig(), workspace=workspace),
        workspace,
        model_name="test-model",
    )


async def _resume(loop: AgentLoop, db: AsyncSession, run_id: str) -> RunOutcome:
    run, raw = await TraceRecorder(db).load_resumable(run_id)
    return await loop.resume(run, LoopState.from_dict(raw))


async def test_ask_user_pauses_with_question_and_deadline(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider([_ask_turn("какой фреймворк?"), _final("готово")])
    outcome = await _loop(provider, db, tmp_path).run("задача", AutonomyMode.YOLO)

    assert outcome.state is RunState.WAITING_APPROVAL
    approval = (await db.execute(select(Approval))).scalar_one()
    assert approval.action_type == "user.question"
    assert approval.payload["question"] == "какой фреймворк?"
    assert approval.payload["deadline"]  # дедлайн проставлен


async def test_answer_returned_to_model_on_resume(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider([_ask_turn(), _final("учёл ответ")])
    loop = _loop(provider, db, tmp_path)
    outcome = await loop.run("задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.WAITING_APPROVAL

    recorder = TraceRecorder(db)
    approval = (await db.execute(select(Approval))).scalar_one()
    await recorder.answer_question(approval, answer="синий", answered_by="test")

    resumed = await _resume(loop, db, outcome.run_id)
    assert resumed.state is RunState.COMPLETED
    # Модель получила ответ человека как результат вызова.
    last_request = provider.seen_messages[-1]
    assert "ответ пользователя: синий" in last_request[-1].content


async def test_empty_answer_signals_proceed(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider([_ask_turn(), _final("продолжил сам")])
    loop = _loop(provider, db, tmp_path)
    outcome = await loop.run("задача", AutonomyMode.YOLO)

    approval = (await db.execute(select(Approval))).scalar_one()
    await TraceRecorder(db).answer_question(approval, answer="", answered_by="test")

    resumed = await _resume(loop, db, outcome.run_id)
    assert resumed.state is RunState.COMPLETED
    assert "по своему усмотрению" in provider.seen_messages[-1][-1].content


async def test_timeout_lets_agent_proceed(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider([_ask_turn(), _final("допущение зафиксировано")])
    loop = _loop(provider, db, tmp_path)
    outcome = await loop.run("задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.WAITING_APPROVAL

    # Симулируем истёкший дедлайн: ответа так и нет.
    approval = (await db.execute(select(Approval))).scalar_one()
    approval.payload = {
        **approval.payload,
        "deadline": (utcnow() - timedelta(seconds=1)).isoformat(),
    }
    await db.commit()

    resumed = await _resume(loop, db, outcome.run_id)
    assert resumed.state is RunState.COMPLETED
    assert "не ответил" in provider.seen_messages[-1][-1].content
    # Вопрос помечен истёкшим.
    refreshed = (await db.execute(select(Approval))).scalar_one()
    assert refreshed.status is ApprovalStatus.EXPIRED
    # Исход вопроса записан в trace как успешный результат вызова.
    call = (await db.execute(select(ToolCall))).scalar_one()
    assert call.tool_name == "ask_user"


async def test_pending_within_deadline_keeps_waiting(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider([_ask_turn()])
    loop = _loop(provider, db, tmp_path)
    outcome = await loop.run("задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.WAITING_APPROVAL

    # Resume без ответа и до дедлайна — снова ждём, дубликата вопроса нет.
    resumed = await _resume(loop, db, outcome.run_id)
    assert resumed.state is RunState.WAITING_APPROVAL
    approvals = (await db.execute(select(Approval))).scalars().all()
    assert len(approvals) == 1


async def test_ask_user_options_land_in_payload(db: AsyncSession, tmp_path: Path) -> None:
    """options из аргументов tool'а доезжают до payload approval'а — UI
    показывает их человеку списком (выбор стрелочками)."""
    args = '{"question": "какой цвет?", "options": ["красный", "синий", "", 42]}'
    turn = CompletionResult(
        content="",
        tool_calls=(ToolCallRequest(id="q1", name="ask_user", arguments_json=args),),
        usage=Usage(10, 5),
        finish_reason="tool_calls",
    )
    provider = ScriptedProvider([turn, _final("готово")])
    outcome = await _loop(provider, db, tmp_path).run("задача", AutonomyMode.YOLO)

    assert outcome.state is RunState.WAITING_APPROVAL
    approval = (await db.execute(select(Approval))).scalars().one()
    # Пустые строки и не-строки отфильтрованы.
    assert approval.payload["options"] == ["красный", "синий"]


async def test_ask_user_without_options_keeps_payload_clean(
    db: AsyncSession, tmp_path: Path
) -> None:
    provider = ScriptedProvider([_ask_turn(), _final("готово")])
    await _loop(provider, db, tmp_path).run("задача", AutonomyMode.YOLO)
    approval = (await db.execute(select(Approval))).scalars().one()
    assert "options" not in approval.payload
