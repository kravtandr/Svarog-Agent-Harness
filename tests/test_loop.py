"""Тесты agent loop v0: итерации, tool calls, лимиты, запись trace."""

import asyncio
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
from svarog_harness.policy.rules import PolicyRule
from svarog_harness.runtime.loop import AgentLoop
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import (
    Checkpoint,
    Message,
    Run,
    RunState,
    ToolCall,
    ToolCallStatus,
)
from svarog_harness.tools.base import RiskLevel, Tool, ToolResult
from svarog_harness.tools.file_tools import file_tools
from svarog_harness.tools.plan_tools import UpdatePlanTool
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
    plan_update_sink: list[dict[str, object]] | None = None,
    rules: list[PolicyRule] | None = None,
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
        PolicyEngine(
            autonomy=AutonomyMode.YOLO,
            policies=PoliciesConfig(),
            workspace=workspace,
            rules=rules or [],
        ),
        workspace,
        model_name="test-model",
        plan_update_sink=plan_update_sink,
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
    # phases (блок A §5) пишутся вместе с update_progress — meta больше не
    # ограничивается одним ключом model.
    assert run.meta["model"] == "test-model"
    assert run.meta["phases"]["llm_call"]["count"] == 1


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


async def test_phase_timings_land_in_run_meta(db: AsyncSession, tmp_path: Path) -> None:
    """Блок A §5: тайминги фаз пишутся в Run.meta и переживают завершение run."""
    (tmp_path / "a.txt").write_text("содержимое", encoding="utf-8")
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "a.txt"}')
            ),
            _final("готово"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("читай", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    run = await db.get(Run, outcome.run_id)
    assert run is not None
    phases = run.meta["phases"]
    assert phases["llm_call"]["count"] == 2
    assert phases["tool_exec"]["count"] >= 1
    assert phases["last"]


async def test_update_plan_is_saved_in_checkpoint(db: AsyncSession, tmp_path: Path) -> None:
    plan_sink: list[dict[str, object]] = []
    registry = ToolRegistry()
    registry.register(
        UpdatePlanTool(lambda items, note: plan_sink.append({"items": items, "note": note}))
    )
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(
                    id="p1",
                    name="update_plan",
                    arguments_json=(
                        '{"items": ['
                        '{"id": "inspect", "text": "изучить состояние", '
                        '"status": "completed"},'
                        '{"id": "verify", "text": "проверить результат", '
                        '"status": "in_progress"}'
                        '], "note": "план создан"}'
                    ),
                )
            ),
            _final("готово"),
        ]
    )
    loop = _loop(provider, db, tmp_path, registry=registry, plan_update_sink=plan_sink)

    outcome = await loop.run("сложная задача", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    checkpoints = (
        (await db.execute(select(Checkpoint).order_by(Checkpoint.created_at))).scalars().all()
    )
    assert checkpoints[-1].state["plan"] == [
        {"id": "inspect", "text": "изучить состояние", "status": "completed"},
        {"id": "verify", "text": "проверить результат", "status": "in_progress"},
    ]


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


async def test_repaired_call_records_original_in_trace(db: AsyncSession, tmp_path: Path) -> None:
    """Блок A §4: ремонт формы аргументов виден в trace — и что прислала
    модель, и что реально исполнилось."""
    (tmp_path / "a.txt").write_text("содержимое", encoding="utf-8")
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(
                    id="c1",
                    name="read_file",
                    arguments_json='{"arguments": {"path": "a.txt"}}',
                )
            ),
            _final("готово"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("читай", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    calls = (await db.scalars(select(ToolCall).where(ToolCall.run_id == outcome.run_id))).all()
    assert len(calls) == 1
    assert calls[0].status is ToolCallStatus.SUCCEEDED
    assert calls[0].arguments["path"] == "a.txt"
    assert calls[0].arguments["_repairs"] == ["unwrapped"]
    assert "arguments" in calls[0].arguments["_raw"]


async def test_suspends_at_max_iterations(db: AsyncSession, tmp_path: Path) -> None:
    endless = [
        _tool_turn(ToolCallRequest(id=f"c{i}", name="list_dir", arguments_json="{}"))
        for i in range(10)
    ]
    provider = ScriptedProvider(endless)
    # refuel отключён (порог > max) и детектор стагнации поднят (§1.6 ловит
    # идентичные вызовы раньше), чтобы проверить именно стоп-кран max_iterations.
    cfg = RuntimeConfig(max_iterations=3, refuel_after_iterations=5, stagnation_repeats=10)
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

    # После двух повторов loop сдаётся — но НЕ принимает дефектный ответ за
    # финальный: протёкший вызов не исполнялся, значит задача не доведена.
    # Решение принимает человек (регрессия S19).
    assert outcome.state is RunState.SUSPENDED
    assert outcome.error is not None
    assert "валидный финальный ответ" in outcome.error
    assert outcome.iterations == 3


async def test_truncated_answer_is_nudged(db: AsyncSession, tmp_path: Path) -> None:
    truncated = CompletionResult(
        content="Ответ обрывается на полусло", usage=Usage(10, 5), finish_reason="length"
    )
    provider = ScriptedProvider([truncated, _final("Полный ответ")])
    outcome = await _loop(provider, db, tmp_path).run("задача", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    assert outcome.final_answer == "Полный ответ"
    nudge = provider.seen_messages[1][-1]
    assert nudge.role == "user"
    assert "обрезан" in nudge.content


async def test_empty_answer_is_nudged(db: AsyncSession, tmp_path: Path) -> None:
    empty = CompletionResult(content="", usage=Usage(10, 5), finish_reason="stop")
    provider = ScriptedProvider([empty, _final("Готово")])
    outcome = await _loop(provider, db, tmp_path).run("задача", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    assert outcome.final_answer == "Готово"
    nudge = provider.seen_messages[1][-1]
    assert nudge.role == "user"
    assert "пустой ответ" in nudge.content


# --- ADR-0015 §1.2: персистенция больших tool-результатов -------------------


class _NoisyArgs(BaseModel):
    pass


class _NoisyTool(Tool[_NoisyArgs]):
    """Возвращает вывод заведомо длиннее tool_output_context_chars."""

    name = "noisy"
    description = "шумный tool"
    risk_level = RiskLevel.LOW
    args_model = _NoisyArgs

    def __init__(self, payload: str) -> None:
        self.payload = payload

    async def execute(self, args: _NoisyArgs) -> ToolResult:
        return ToolResult.success(self.payload)


async def test_large_tool_output_spilled_to_workspace(db: AsyncSession, tmp_path: Path) -> None:
    payload = "".join(f"строка {i}\n" for i in range(100))
    registry = ToolRegistry()
    registry.register(_NoisyTool(payload))
    provider = ScriptedProvider(
        [
            _tool_turn(ToolCallRequest(id="call-1", name="noisy", arguments_json="{}")),
            _final("готово"),
        ]
    )
    cfg = RuntimeConfig(tool_output_context_chars=200)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg, registry=registry).run(
        "шуми", AutonomyMode.YOLO
    )
    assert outcome.state is RunState.COMPLETED

    tool_message = provider.seen_messages[-1][-1]
    assert tool_message.role == "tool"
    # Модель получает голову + маркер с путём к полному файлу.
    assert "строка 0" in tool_message.content
    assert f"показано 200 из {len(payload)} символов" in tool_message.content
    assert ".svarog/tool-results/" in tool_message.content
    assert "читай read_file частями" in tool_message.content
    assert len(tool_message.content) < len(payload)

    run = (await db.execute(select(Run))).scalar_one()
    spill = tmp_path / ".svarog" / "tool-results" / run.id[:8] / "call-1.txt"
    assert spill.is_file()
    assert spill.read_text(encoding="utf-8") == payload


async def test_read_file_output_truncated_without_spill(db: AsyncSession, tmp_path: Path) -> None:
    """read_file не персистится (петля Read → файл → Read) — честная обрезка."""
    (tmp_path / "big.txt").write_text(
        "".join(f"строка {i}\n" for i in range(100)), encoding="utf-8"
    )
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "big.txt"}')
            ),
            _final("готово"),
        ]
    )
    cfg = RuntimeConfig(tool_output_context_chars=200)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("читай", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    tool_message = provider.seen_messages[-1][-1]
    assert "обрезан" in tool_message.content
    assert "offset" in tool_message.content
    assert not (tmp_path / ".svarog" / "tool-results").exists()


# --- ADR-0015 §1.3: параллельные read-only батчи -----------------------------


class _PairedTool(Tool[_NoisyArgs]):
    """Оба вызова должны стартовать до завершения любого из них.

    При последовательном исполнении первый вызов упрётся в timeout —
    успех обоих доказывает параллельность батча.
    """

    name = "paired"
    description = "ждёт второго участника батча"
    risk_level = RiskLevel.LOW
    timeout_sec = 2.0
    args_model = _NoisyArgs

    def __init__(self) -> None:
        self.started = 0
        self._both_started = asyncio.Event()

    def is_read_only(self, args: _NoisyArgs) -> bool:
        return True

    async def execute(self, args: _NoisyArgs) -> ToolResult:
        self.started += 1
        if self.started >= 2:
            self._both_started.set()
        await self._both_started.wait()
        return ToolResult.success("парный вызов исполнен")


async def test_read_only_calls_execute_concurrently(db: AsyncSession, tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_PairedTool())
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="p1", name="paired", arguments_json="{}"),
                ToolCallRequest(id="p2", name="paired", arguments_json="{}"),
            ),
            _final("готово"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path, registry=registry).run(
        "парный вызов", AutonomyMode.YOLO
    )
    assert outcome.state is RunState.COMPLETED

    tool_messages = [m for m in provider.seen_messages[-1] if m.role == "tool"]
    assert len(tool_messages) == 2
    assert all("парный вызов исполнен" in m.content for m in tool_messages)


async def test_batch_results_in_order_single_checkpoint(db: AsyncSession, tmp_path: Path) -> None:
    """Результаты батча — в исходном порядке; checkpoint — один на батч."""
    (tmp_path / "a.txt").write_text("содержимое A", encoding="utf-8")
    (tmp_path / "b.txt").write_text("содержимое B", encoding="utf-8")
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "a.txt"}'),
                ToolCallRequest(id="c2", name="read_file", arguments_json='{"path": "b.txt"}'),
            ),
            _final("готово"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("читай оба", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    tool_messages = [m for m in provider.seen_messages[-1] if m.role == "tool"]
    assert [m.tool_call_id for m in tool_messages] == ["c1", "c2"]
    assert "содержимое A" in tool_messages[0].content
    assert "содержимое B" in tool_messages[1].content

    # Checkpoints: стартовый + write-ahead + один на весь батч (не по одному на вызов).
    checkpoints = (await db.execute(select(Checkpoint))).scalars().all()
    assert len(checkpoints) == 3


async def test_write_call_not_batched_with_reads(db: AsyncSession, tmp_path: Path) -> None:
    """Мутирующий вызов исполняется последовательно; порядок результатов сохранён."""
    (tmp_path / "a.txt").write_text("до", encoding="utf-8")
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="r1", name="read_file", arguments_json='{"path": "a.txt"}'),
                ToolCallRequest(
                    id="w1",
                    name="write_file",
                    arguments_json='{"path": "new.txt", "content": "после"}',
                ),
                ToolCallRequest(id="r2", name="read_file", arguments_json='{"path": "new.txt"}'),
            ),
            _final("готово"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("смешанный батч", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    tool_messages = [m for m in provider.seen_messages[-1] if m.role == "tool"]
    assert [m.tool_call_id for m in tool_messages] == ["r1", "w1", "r2"]
    # r2 видит результат w1 — записи не «уезжают» в параллель с чтениями.
    assert "после" in tool_messages[2].content


# --- ADR-0015 §1.4: микрокомпакция — очистка старых tool-результатов ---------


async def test_microcompact_clears_old_tool_results(db: AsyncSession, tmp_path: Path) -> None:
    """Порог контекста превышен → старые tool-результаты очищаются маркером,
    защищённый хвост не трогается, структура истории сохраняется."""
    big = "\n".join(f"строка {i}: " + "х" * 40 for i in range(20))  # > 500 символов
    (tmp_path / "a.txt").write_text(big, encoding="utf-8")
    (tmp_path / "b.txt").write_text(big, encoding="utf-8")
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "a.txt"}'),
                usage=Usage(600, 5),
            ),
            _tool_turn(
                ToolCallRequest(id="c2", name="read_file", arguments_json='{"path": "b.txt"}'),
                usage=Usage(600, 5),
            ),
            _final("готово"),
        ]
    )
    cfg = RuntimeConfig(
        max_context_tokens=1000,
        microcompact_threshold_ratio=0.5,
        microcompact_keep_recent=1,
    )
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("читай", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    final_request = provider.seen_messages[-1]
    tool_messages = [m for m in final_request if m.role == "tool"]
    assert len(tool_messages) == 2
    # Старый результат очищен, но структура истории цела (role/tool_call_id на месте).
    assert tool_messages[0].tool_call_id == "c1"
    assert "очищен для экономии контекста" in tool_messages[0].content
    assert "read_file" in tool_messages[0].content
    assert "строка 1" not in tool_messages[0].content
    # Защищённый хвост (keep_recent=1) не тронут.
    assert "строка 1" in tool_messages[1].content


async def test_microcompact_skips_short_results_and_below_threshold(
    db: AsyncSession, tmp_path: Path
) -> None:
    (tmp_path / "short.txt").write_text("коротко", encoding="utf-8")
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "short.txt"}'),
                usage=Usage(600, 5),
            ),
            _tool_turn(
                ToolCallRequest(id="c2", name="read_file", arguments_json='{"path": "short.txt"}'),
                usage=Usage(600, 5),
            ),
            _final("готово"),
        ]
    )
    cfg = RuntimeConfig(
        max_context_tokens=1000,
        microcompact_threshold_ratio=0.5,
        microcompact_keep_recent=0,
    )
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("читай", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED
    # Сообщения < 500 символов не чистятся даже при превышении порога.
    tool_messages = [m for m in provider.seen_messages[-1] if m.role == "tool"]
    assert all("коротко" in m.content for m in tool_messages)


async def test_microcompact_marker_references_spill_file(db: AsyncSession, tmp_path: Path) -> None:
    """Есть spill-файл из §1.2 → маркер очистки ссылается на него (данные не теряются)."""
    payload = "".join(f"строка {i}\n" for i in range(200))
    registry = ToolRegistry()
    registry.register(_NoisyTool(payload))
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="n1", name="noisy", arguments_json="{}"),
                usage=Usage(600, 5),
            ),
            _tool_turn(
                ToolCallRequest(id="n2", name="noisy", arguments_json="{}"),
                usage=Usage(600, 5),
            ),
            _final("готово"),
        ]
    )
    cfg = RuntimeConfig(
        max_context_tokens=1000,
        microcompact_threshold_ratio=0.5,
        microcompact_keep_recent=1,
        tool_output_context_chars=600,
    )
    outcome = await _loop(provider, db, tmp_path, cfg=cfg, registry=registry).run(
        "шуми", AutonomyMode.YOLO
    )
    assert outcome.state is RunState.COMPLETED

    tool_messages = [m for m in provider.seen_messages[-1] if m.role == "tool"]
    cleared = tool_messages[0].content
    assert "очищен для экономии контекста" in cleared
    assert ".svarog/tool-results/" in cleared  # ссылка на полный вывод


async def test_microcompact_marker_is_actionable(db: AsyncSession, tmp_path: Path) -> None:
    """Без spill-файла маркер не предлагает повторить ТОТ ЖЕ вызов, а требует
    сузить параметры (блок A §2): иначе модель зацикливается на повторе."""
    big = "\n".join(f"строка {i}: " + "х" * 40 for i in range(20))  # > 500 символов
    (tmp_path / "a.txt").write_text(big, encoding="utf-8")
    (tmp_path / "b.txt").write_text(big, encoding="utf-8")
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "a.txt"}'),
                usage=Usage(600, 5),
            ),
            _tool_turn(
                ToolCallRequest(id="c2", name="read_file", arguments_json='{"path": "b.txt"}'),
                usage=Usage(600, 5),
            ),
            _final("готово"),
        ]
    )
    cfg = RuntimeConfig(
        max_context_tokens=1000,
        microcompact_threshold_ratio=0.5,
        microcompact_keep_recent=1,
    )
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("читай", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    cleared = next(m for m in provider.seen_messages[-1] if m.role == "tool")
    assert "более узкими параметрами" in cleared.content
    assert "повтори вызов при необходимости" not in cleared.content


# --- ADR-0015 §1.6: детектор затухающей отдачи --------------------------------


class _CountingTool(Tool[_NoisyArgs]):
    """Возвращает разный результат на каждый вызов — прогресс есть."""

    name = "counting"
    description = "счётчик"
    risk_level = RiskLevel.LOW
    args_model = _NoisyArgs

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, args: _NoisyArgs) -> ToolResult:
        self.calls += 1
        return ToolResult.success(f"вызов номер {self.calls}")


def _same_read(call_id: str) -> CompletionResult:
    return _tool_turn(
        ToolCallRequest(id=call_id, name="read_file", arguments_json='{"path": "same.txt"}')
    )


async def test_stagnation_identical_calls_suspend(db: AsyncSession, tmp_path: Path) -> None:
    """3 подряд идентичных вызова с идентичным результатом → suspended, не failed."""
    (tmp_path / "same.txt").write_text("неизменно", encoding="utf-8")
    provider = ScriptedProvider([_same_read("c1"), _same_read("c2"), _same_read("c3")])
    cfg = RuntimeConfig(stagnation_repeats=3)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("зациклись", AutonomyMode.YOLO)

    assert outcome.state is RunState.SUSPENDED
    assert outcome.error is not None
    assert "затухающая отдача" in outcome.error
    assert "read_file" in outcome.error
    assert "resume" in outcome.error


async def test_no_stagnation_when_results_differ(db: AsyncSession, tmp_path: Path) -> None:
    """Поллинг с меняющимся выводом — не стагнация (вызовы не идентичны)."""
    registry = ToolRegistry()
    registry.register(_CountingTool())
    call = ToolCallRequest(id="c", name="counting", arguments_json="{}")
    provider = ScriptedProvider(
        [
            _tool_turn(call),
            _tool_turn(call),
            _tool_turn(call),
            _final("дождался", usage=Usage(10, 600)),
        ]
    )
    cfg = RuntimeConfig(stagnation_repeats=3)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg, registry=registry).run(
        "поллинг", AutonomyMode.YOLO
    )
    assert outcome.state is RunState.COMPLETED


async def test_stagnation_token_fading_suspends(db: AsyncSession, tmp_path: Path) -> None:
    """Итерации без успешных tool-результатов и с малой дельтой вывода → suspended."""
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="f1", name="read_file", arguments_json='{"path": "no1.txt"}')
            ),
            _tool_turn(
                ToolCallRequest(id="f2", name="read_file", arguments_json='{"path": "no2.txt"}')
            ),
            _tool_turn(
                ToolCallRequest(id="f3", name="read_file", arguments_json='{"path": "no3.txt"}')
            ),
        ]
    )
    cfg = RuntimeConfig(stagnation_repeats=3)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("тупик", AutonomyMode.YOLO)

    assert outcome.state is RunState.SUSPENDED
    assert outcome.error is not None
    assert "затухающая отдача" in outcome.error


async def test_successful_progress_resets_fading_counter(db: AsyncSession, tmp_path: Path) -> None:
    """Успешные tool-результаты обнуляют счётчик затухания."""
    (tmp_path / "ok.txt").write_text("есть", encoding="utf-8")
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="f1", name="read_file", arguments_json='{"path": "no1.txt"}')
            ),
            _tool_turn(
                ToolCallRequest(id="f2", name="read_file", arguments_json='{"path": "no2.txt"}')
            ),
            _tool_turn(
                ToolCallRequest(id="ok", name="read_file", arguments_json='{"path": "ok.txt"}')
            ),
            _tool_turn(
                ToolCallRequest(id="f3", name="read_file", arguments_json='{"path": "no3.txt"}')
            ),
            _final("разобрался"),
        ]
    )
    cfg = RuntimeConfig(stagnation_repeats=3)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("почти тупик", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED


# --- ADR-0015 фаза 5: cost/context-индикатор (on_progress) --------------------


async def test_on_progress_reports_each_iteration(db: AsyncSession, tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="c1", name="list_dir", arguments_json="{}"),
                usage=Usage(1000, 5),
            ),
            _final("готово", usage=Usage(2000, 5)),
        ]
    )
    progress: list[tuple[int, int, float, float, int]] = []
    cfg = RuntimeConfig(max_context_tokens=10_000)
    registry = ToolRegistry()
    for tool in file_tools(tmp_path):
        registry.register(tool)
    loop = AgentLoop(
        provider,
        registry,
        TraceRecorder(db),
        cfg,
        PolicyEngine(autonomy=AutonomyMode.YOLO, policies=PoliciesConfig(), workspace=tmp_path),
        tmp_path,
        model_name="test-model",
        on_progress=lambda i, tok, cost, ratio, cached: progress.append(
            (i, tok, cost, ratio, cached)
        ),
    )
    outcome = await loop.run("задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED
    assert len(progress) == 2
    iters = [p[0] for p in progress]
    assert iters == [1, 2]
    # Доля контекста — prompt_tokens последнего ответа к max_context_tokens.
    assert progress[0][3] == pytest.approx(0.1)
    assert progress[1][3] == pytest.approx(0.2)
    # Токены накапливаются.
    assert progress[1][1] == outcome.tokens_used


# --- Блок A §3: cached_tokens — учёт эффекта стабильного префикса схем -------


async def test_cached_tokens_accumulate_in_run_meta(db: AsyncSession, tmp_path: Path) -> None:
    """Блок A §3: cached-токены копятся в Run.meta и видны в trace."""
    provider = ScriptedProvider(
        [
            _final("готово", usage=Usage(prompt_tokens=100, completion_tokens=5, cached_tokens=64)),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    run = await db.get(Run, outcome.run_id)
    assert run is not None
    assert run.meta["cached_tokens"] == 64


async def test_cached_tokens_accumulate_across_iterations(db: AsyncSession, tmp_path: Path) -> None:
    """Блок A §3: cached-токены суммируются через несколько итераций."""
    # Подготовка файла для инструмента.
    (tmp_path / "note.txt").write_text("текст", encoding="utf-8")
    provider = ScriptedProvider(
        [
            # Первая итерация: вызов инструмента с кэшированными токенами.
            _tool_turn(
                ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "note.txt"}'),
                usage=Usage(prompt_tokens=100, completion_tokens=0, cached_tokens=32),
            ),
            # Вторая итерация: финальный ответ тоже с кэшированными токенами.
            _final(
                "Содержимое файла: текст",
                usage=Usage(prompt_tokens=50, completion_tokens=5, cached_tokens=16),
            ),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("прочитай файл", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED
    assert outcome.iterations == 2

    # cached_tokens должны быть суммой: 32 + 16 = 48.
    run = await db.get(Run, outcome.run_id)
    assert run is not None
    assert run.meta["cached_tokens"] == 48


# --- Блок E §3: подсказка при отказе на жёсткой границе ----------------------


async def test_boundary_note_reaches_model_but_not_trace(db: AsyncSession, tmp_path: Path) -> None:
    """Подсказка уходит модели, в trace остаётся чистая причина."""
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(
                    id="c1", name="read_file", arguments_json='{"path": "../снаружи.txt"}'
                )
            ),
            _final("готово"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("читай", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    # Модель видит причину и подсказку.
    tool_messages = [m for m in provider.seen_messages[-1] if m.role == "tool"]
    assert tool_messages
    assert "выходит за пределы" in tool_messages[0].content
    assert "ask_user" in tool_messages[0].content

    # В trace — только причина, без текста-инструкции.
    tool_call = (await db.execute(select(ToolCall))).scalar_one()
    assert tool_call.error is not None
    assert "выходит за пределы" in tool_call.error
    assert "ask_user" not in tool_call.error


async def test_ordinary_failure_gets_no_note(db: AsyncSession, tmp_path: Path) -> None:
    """Обычная ошибка tool не получает подсказки: там повтор осмыслен."""
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(
                    id="c1", name="read_file", arguments_json='{"path": "нет-такого.txt"}'
                )
            ),
            _final("готово"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("читай", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    tool_messages = [m for m in provider.seen_messages[-1] if m.role == "tool"]
    assert tool_messages
    assert "ask_user" not in tool_messages[0].content
    assert "жёсткая граница" not in tool_messages[0].content


async def test_policy_deny_explains_boundary_to_model(db: AsyncSession, tmp_path: Path) -> None:
    """Блок E §3: запрет политикой объясняет, что повтор даст тот же отказ."""
    rules = [
        PolicyRule(match="file.*", decision="deny", reason="инфраструктура", paths=["infra/*"])
    ]
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(
                    id="c1",
                    name="write_file",
                    arguments_json='{"path": "infra/main.tf", "content": "x"}',
                )
            ),
            _final("понял, не трогаю"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path, rules=rules).run("правь infra", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    model_text = next(m for m in provider.seen_messages[-1] if m.role == "tool").content
    assert "инфраструктура" in model_text
    assert "request_approval" in model_text

    tool_call = (await db.execute(select(ToolCall))).scalar_one()
    assert tool_call.error is not None
    assert "инфраструктура" in tool_call.error
    assert "request_approval" not in tool_call.error


# --- Блок B: автопродолжение после refuel ------------------------------------


async def test_max_iterations_limits_segment_not_run(db: AsyncSession, tmp_path: Path) -> None:
    """Блок B §2: max_iterations ограничивает сегмент между refuel'ами.

    Сегмент = 2 итерации, потолок раундов = 2, лимит сегмента = 3. Run
    проходит 5 итераций — больше max_iterations — и завершается сам.
    """
    for name in ("a", "b", "c", "d"):
        (tmp_path / f"{name}.txt").write_text(f"содержимое {name}", encoding="utf-8")
    # Вызовы должны различаться: одинаковые подряд ловит детектор затухающей
    # отдачи (ADR-0015 §1.6) и приостанавливает run раньше refuel.
    calls = [
        ToolCallRequest(id=f"c{i}", name="read_file", arguments_json=f'{{"path": "{n}.txt"}}')
        for i, n in enumerate(("a", "b", "c", "d"))
    ]
    provider = ScriptedProvider([_tool_turn(c) for c in calls] + [_final("готово")])
    cfg = RuntimeConfig(max_iterations=3, refuel_after_iterations=2, max_refuel_rounds=2)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("работай", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    assert outcome.iterations == 5


async def test_autocontinue_finishes_task_without_manual_resume(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Блок B §3: run сам продолжает работу после сброса контекста."""
    for name in ("a", "b", "c", "d"):
        (tmp_path / f"{name}.txt").write_text(f"содержимое {name}", encoding="utf-8")
    # Вызовы должны различаться: одинаковые подряд ловит детектор затухающей
    # отдачи (ADR-0015 §1.6) и приостанавливает run раньше refuel.
    calls = [
        ToolCallRequest(id=f"c{i}", name="read_file", arguments_json=f'{{"path": "{n}.txt"}}')
        for i, n in enumerate(("a", "b", "c", "d"))
    ]
    provider = ScriptedProvider([_tool_turn(c) for c in calls] + [_final("готово")])
    cfg = RuntimeConfig(max_iterations=10, refuel_after_iterations=2, max_refuel_rounds=2)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("работай", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    assert outcome.final_answer == "готово"

    run = await db.get(Run, outcome.run_id)
    assert run is not None
    assert run.meta["refuel_rounds"] == 2

    # Контекст действительно пересобирался: после сброса первым идёт system,
    # вторым — user с сохранённым состоянием задачи.
    third_request = provider.seen_messages[2]
    assert third_request[0].role == "system"
    assert "Task state" in third_request[1].content


async def test_autocontinue_stops_at_round_cap(db: AsyncSession, tmp_path: Path) -> None:
    """Потолок раундов исчерпан — run приостанавливается с внятной причиной."""
    for name in ("a", "b", "c", "d"):
        (tmp_path / f"{name}.txt").write_text(f"содержимое {name}", encoding="utf-8")
    # Вызовы должны различаться: одинаковые подряд ловит детектор затухающей
    # отдачи (ADR-0015 §1.6) и приостанавливает run раньше refuel.
    calls = [
        ToolCallRequest(id=f"c{i}", name="read_file", arguments_json=f'{{"path": "{n}.txt"}}')
        for i, n in enumerate(("a", "b", "c", "d"))
    ]
    provider = ScriptedProvider([_tool_turn(c) for c in calls] + [_final("готово")])
    cfg = RuntimeConfig(max_iterations=10, refuel_after_iterations=2, max_refuel_rounds=1)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("работай", AutonomyMode.YOLO)

    assert outcome.state is RunState.SUSPENDED
    assert outcome.error is not None
    assert "max_refuel_rounds" in outcome.error


async def test_zero_round_cap_keeps_old_suspend_behaviour(db: AsyncSession, tmp_path: Path) -> None:
    """max_refuel_rounds=0 — прежнее поведение: приостановка на первом refuel."""
    for name in ("a", "b", "c", "d"):
        (tmp_path / f"{name}.txt").write_text(f"содержимое {name}", encoding="utf-8")
    # Вызовы должны различаться: одинаковые подряд ловит детектор затухающей
    # отдачи (ADR-0015 §1.6) и приостанавливает run раньше refuel.
    calls = [
        ToolCallRequest(id=f"c{i}", name="read_file", arguments_json=f'{{"path": "{n}.txt"}}')
        for i, n in enumerate(("a", "b", "c", "d"))
    ]
    provider = ScriptedProvider([_tool_turn(calls[0]), _tool_turn(calls[1]), _final("готово")])
    cfg = RuntimeConfig(max_iterations=10, refuel_after_iterations=2, max_refuel_rounds=0)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("работай", AutonomyMode.YOLO)

    assert outcome.state is RunState.SUSPENDED
    assert outcome.error is not None
    assert "task_state.md" in outcome.error
    assert (tmp_path / "task_state.md").exists()


async def test_exhausted_nudges_suspend_instead_of_completing(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Модель молчит — run НЕ считается успешным (регрессия S19).

    Исчерпание nudge'ей означает, что валидного финального ответа так и не
    получено. Помечать это `completed` — значит рапортовать успех о
    недоделанной задаче; решение принимает человек, как при стагнации.
    """
    provider = ScriptedProvider(
        [
            CompletionResult(content="", usage=Usage(10, 5), finish_reason="stop"),
            CompletionResult(content="", usage=Usage(10, 5), finish_reason="stop"),
            CompletionResult(content="", usage=Usage(10, 5), finish_reason="stop"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("сделай что-нибудь", AutonomyMode.YOLO)

    assert outcome.state is RunState.SUSPENDED
    assert outcome.error is not None
    assert "пуст" in outcome.error.lower() or "финальн" in outcome.error.lower()


async def test_valid_final_answer_still_completes(db: AsyncSession, tmp_path: Path) -> None:
    """Страховка: обычное завершение с содержательным ответом не сломано."""
    provider = ScriptedProvider([_final("готово")])
    outcome = await _loop(provider, db, tmp_path).run("задача", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    assert outcome.final_answer == "готово"
