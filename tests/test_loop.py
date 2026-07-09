"""Тесты agent loop v0: итерации, tool calls, лимиты, запись trace."""

from collections.abc import AsyncIterator, Callable
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
from svarog_harness.runtime.loop import AgentLoop
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import Message, Run, RunState, ToolCall, ToolCallStatus
from svarog_harness.tools.file_tools import file_tools
from svarog_harness.tools.registry import ToolRegistry
from svarog_harness.trace.recorder import TraceRecorder


class ScriptedProvider(ModelProvider):
    """Возвращает заранее заданные ответы по очереди."""

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
        result = self.turns.pop(0)
        if on_text_delta is not None and result.content:
            on_text_delta(result.content)
        return result


def _final(text: str, *, usage: Usage | None = None, cost: float = 0.0) -> CompletionResult:
    return CompletionResult(
        content=text, usage=usage or Usage(10, 5), cost_usd=cost, finish_reason="stop"
    )


def _tool_turn(*calls: ToolCallRequest, usage: Usage | None = None) -> CompletionResult:
    return CompletionResult(
        content="", tool_calls=calls, usage=usage or Usage(10, 5), finish_reason="tool_calls"
    )


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
    *,
    cfg: RuntimeConfig | None = None,
    registry: ToolRegistry | None = None,
) -> AgentLoop:
    if registry is None:
        registry = ToolRegistry()
        for tool in file_tools(workspace):
            registry.register(tool)
    return AgentLoop(
        provider,
        registry,
        TraceRecorder(db),
        cfg or RuntimeConfig(),
        PolicyEngine(autonomy=AutonomyMode.YOLO, policies=PoliciesConfig(), workspace=workspace),
        workspace,
        model_name="test-model",
    )


async def test_completes_on_final_answer(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider([_final("Готово", cost=0.01)])
    outcome = await _loop(provider, db, tmp_path).run("скажи готово", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    assert outcome.final_answer == "Готово"
    assert outcome.iterations == 1
    assert outcome.tokens_used == 15
    assert outcome.cost_usd == pytest.approx(0.01)

    run = (await db.execute(select(Run))).scalar_one()
    assert run.state is RunState.COMPLETED
    assert run.autonomy == "yolo"
    assert run.task == "скажи готово"
    assert run.finished_at is not None
    assert run.meta == {"model": "test-model"}


async def test_executes_tool_calls_and_feeds_results_back(db: AsyncSession, tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("секретное число: 17", encoding="utf-8")
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "note.txt"}')
            ),
            _final("Число: 17"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("прочитай note.txt", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    assert outcome.iterations == 2
    # Результат tool вернулся в следующий запрос к модели.
    last_request = provider.seen_messages[-1]
    assert last_request[-1].role == "tool"
    assert "17" in last_request[-1].content

    tool_call = (await db.execute(select(ToolCall))).scalar_one()
    assert tool_call.tool_name == "read_file"
    assert tool_call.status is ToolCallStatus.SUCCEEDED
    assert tool_call.risk_level == "low"
    assert tool_call.arguments == {"path": "note.txt"}
    assert tool_call.result is not None
    assert "17" in tool_call.result["output"]


async def test_records_full_message_trace(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="c1", name="list_dir", arguments_json="{}"),
            ),
            _final("пусто"),
        ]
    )
    await _loop(provider, db, tmp_path).run("что в workspace?", AutonomyMode.AUTO)

    messages = (await db.execute(select(Message).order_by(Message.index_in_run))).scalars().all()
    roles = [m.role for m in messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert messages[2].content["tool_calls"][0]["name"] == "list_dir"
    assert messages[3].content["tool_call_id"] == "c1"


async def test_unknown_tool_reported_to_model(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            _tool_turn(ToolCallRequest(id="c1", name="teleport", arguments_json="{}")),
            _final("ок, без телепорта"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("телепортируйся", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    last_request = provider.seen_messages[-1]
    assert "неизвестный tool" in last_request[-1].content

    tool_call = (await db.execute(select(ToolCall))).scalar_one()
    assert tool_call.status is ToolCallStatus.FAILED


async def test_invalid_arguments_json_reported_to_model(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            _tool_turn(ToolCallRequest(id="c1", name="list_dir", arguments_json="{broken")),
            _final("повторю позже"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("сломанный вызов", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    tool_call = (await db.execute(select(ToolCall))).scalar_one()
    assert tool_call.status is ToolCallStatus.FAILED
    assert tool_call.arguments == {"_raw": "{broken"}


async def test_suspends_at_max_iterations(db: AsyncSession, tmp_path: Path) -> None:
    endless = [
        _tool_turn(ToolCallRequest(id=f"c{i}", name="list_dir", arguments_json="{}"))
        for i in range(10)
    ]
    provider = ScriptedProvider(endless)
    cfg = RuntimeConfig(max_iterations=3, refuel_after_iterations=2)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("зациклись", AutonomyMode.YOLO)

    assert outcome.state is RunState.SUSPENDED
    assert outcome.iterations == 3
    assert outcome.error is not None
    assert "лимит итераций" in outcome.error


async def test_suspends_when_cost_budget_exceeded(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            CompletionResult(
                content="",
                tool_calls=(ToolCallRequest(id="c1", name="list_dir", arguments_json="{}"),),
                usage=Usage(10, 5),
                cost_usd=9.99,
            ),
            _final("не должно дойти"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("дорогая задача", AutonomyMode.YOLO)

    assert outcome.state is RunState.SUSPENDED
    assert outcome.error is not None
    assert "бюджет стоимости" in outcome.error
    run = (await db.execute(select(Run))).scalar_one()
    assert run.state is RunState.SUSPENDED
    assert run.finished_at is None  # suspended — не терминальное состояние
    assert run.cost_usd == pytest.approx(9.99)


async def test_suspends_when_context_overflows(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="c1", name="list_dir", arguments_json="{}"),
                usage=Usage(prompt_tokens=999_999, completion_tokens=1),
            ),
            _final("не должно дойти"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("огромный контекст", AutonomyMode.YOLO)

    assert outcome.state is RunState.SUSPENDED
    assert outcome.error is not None
    assert "контекст превысил лимит" in outcome.error


async def test_provider_exception_fails_run(db: AsyncSession, tmp_path: Path) -> None:
    class BrokenProvider(ModelProvider):
        async def complete(
            self,
            messages: list[ChatMessage],
            tools: list[ToolDefinition],
            *,
            on_text_delta: Callable[[str], None] | None = None,
        ) -> CompletionResult:
            raise ConnectionError("сервер недоступен")

    outcome = await _loop(BrokenProvider(), db, tmp_path).run("задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.FAILED
    assert outcome.error is not None
    assert "сервер недоступен" in outcome.error
    run = (await db.execute(select(Run))).scalar_one()
    assert run.state is RunState.FAILED


def _leaked_final(text: str) -> CompletionResult:
    """«Финальный» ответ, в котором провайдер заподозрил протёкший tool call."""
    return CompletionResult(
        content=text, usage=Usage(10, 5), finish_reason="stop", leak_suspected=True
    )


async def test_leak_suspected_answer_is_nudged_not_completed(
    db: AsyncSession, tmp_path: Path
) -> None:
    leaked = "commentary to=functions.remember json{'file':} final Запомнил."
    provider = ScriptedProvider([_leaked_final(leaked), _final("Готово")])
    outcome = await _loop(provider, db, tmp_path).run("запомни факт", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    assert outcome.final_answer == "Готово"
    assert outcome.iterations == 2
    # Вторая итерация получила корректирующее сообщение вместо завершения run.
    nudge = provider.seen_messages[1][-1]
    assert nudge.role == "user"
    assert "НЕ был исполнен" in nudge.content


async def test_leak_nudges_are_capped(db: AsyncSession, tmp_path: Path) -> None:
    leaked = "to=functions.remember json{broken"
    provider = ScriptedProvider([_leaked_final(leaked)] * 3)
    outcome = await _loop(provider, db, tmp_path).run("запомни факт", AutonomyMode.YOLO)

    # После двух повторов loop сдаётся и принимает ответ как финальный.
    assert outcome.state is RunState.COMPLETED
    assert outcome.final_answer == leaked
    assert outcome.iterations == 3
