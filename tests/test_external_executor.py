"""Тесты внешнего executor'а (ADR-0016 фаза 1): адаптер claude-code,
стрим → trace, redaction, fail-closed гейты."""

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import AutonomyMode, ExecutorConfig, ExternalExecutorConfig
from svarog_harness.runtime.agents import adapter_for
from svarog_harness.runtime.agents.claude_code import ClaudeCodeAdapter
from svarog_harness.runtime.config_snapshot import config_digest
from svarog_harness.runtime.external import ExternalAgentExecutor
from svarog_harness.runtime.orchestrator import RunHooks, TaskRunner
from svarog_harness.sandbox import SandboxError
from svarog_harness.sandbox.local import LocalEnvironment
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import Message, Run, RunState, ToolCall, ToolCallStatus
from svarog_harness.trace.recorder import TraceRecorder

# ---------------------------------------------------------------------------
# Golden-события стрима Claude Code (ADR-0016 §8): фиксируют контракт парсера.

_INIT = {"type": "system", "subtype": "init", "session_id": "sess-1", "tools": ["Bash"]}
_ASSISTANT = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "смотрю workspace"},
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
        ],
    },
    "session_id": "sess-1",
}
_TOOL_RESULT = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": [{"type": "text", "text": "hello.py"}],
                "is_error": False,
            }
        ],
    },
    "session_id": "sess-1",
}
_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "готово: hello.py создан",
    "num_turns": 3,
    "total_cost_usd": 0.05,
    "usage": {"input_tokens": 100, "output_tokens": 40},
    "session_id": "sess-1",
}


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


# --- Адаптер: парсинг stream-json -----------------------------------------


def test_parse_assistant_text_and_tool_use() -> None:
    events = ClaudeCodeAdapter().parse_event(json.dumps(_ASSISTANT))
    assert [e.kind for e in events] == ["text", "tool_call"]
    text, call = events
    assert text.text == "смотрю workspace"
    assert call.tool_name == "Bash"
    assert call.call_id == "toolu_1"
    assert call.arguments == {"command": "ls"}
    assert call.session_id == "sess-1"


def test_parse_tool_result() -> None:
    (event,) = ClaudeCodeAdapter().parse_event(json.dumps(_TOOL_RESULT))
    assert event.kind == "tool_result"
    assert event.call_id == "toolu_1"
    assert event.text == "hello.py"
    assert event.ok


def test_parse_result_totals() -> None:
    (event,) = ClaudeCodeAdapter().parse_event(json.dumps(_RESULT))
    assert event.kind == "result"
    assert event.ok
    assert event.text == "готово: hello.py создан"
    assert (event.input_tokens, event.output_tokens) == (100, 40)
    assert event.cost_usd == 0.05
    assert event.num_turns == 3


def test_parse_unknown_type_is_opaque_with_raw() -> None:
    payload = {"type": "stream_event", "data": {"x": 1}}
    (event,) = ClaudeCodeAdapter().parse_event(json.dumps(payload))
    assert event.kind == "opaque"
    assert event.raw == payload


def test_parse_noise_lines_skipped() -> None:
    adapter = ClaudeCodeAdapter()
    assert adapter.parse_event("") == []
    assert adapter.parse_event("не json вовсе") == []
    assert adapter.parse_event("[1, 2]") == []


def test_command_flags_and_resume() -> None:
    adapter = ClaudeCodeAdapter()
    argv = adapter.command("сделай hello.py")
    assert argv[:3] == ["claude", "-p", "сделай hello.py"]
    assert "stream-json" in argv
    assert "bypassPermissions" in argv  # tier 1: границу держит sandbox
    assert "--resume" not in argv
    assert adapter.command("t", session="sess-9")[-2:] == ["--resume", "sess-9"]


# --- Конфигурация -----------------------------------------------------------


def test_external_type_requires_section() -> None:
    with pytest.raises(ValidationError, match=r"executor\.external"):
        ExecutorConfig(type="external")


def test_adapter_registry() -> None:
    adapter = adapter_for(ExternalExecutorConfig(image="img:1"))
    assert adapter.name == "claude-code"


def test_config_digest_covers_executor(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    native = load_config(project_dir=ws)
    external = native.model_copy(
        update={
            "executor": ExecutorConfig(
                type="external", external=ExternalExecutorConfig(image="img:1")
            )
        }
    )
    # Подмена data-plane между стартом и resume должна ловиться trust gate (§0.4).
    assert config_digest(native, ws) != config_digest(external, ws)


# --- Executor: скриптовый агент через LocalEnvironment ----------------------


class _ScriptAdapter(ClaudeCodeAdapter):
    """Парсинг настоящий (claude-code); команда — локальный скрипт-агент."""

    def __init__(self, argv: list[str]) -> None:
        super().__init__()
        self._argv = argv

    def command(self, task: str, *, session: str | None = None) -> list[str]:
        return list(self._argv)


def _agent_script(
    tmp_path: Path,
    lines: list[dict[str, Any]],
    *,
    exit_code: int = 0,
    sleep_before: float = 0.0,
) -> list[str]:
    script = tmp_path / "fake_agent.py"
    script.write_text(
        "import json, sys, time\n"
        f"time.sleep({sleep_before})\n"
        f"for obj in json.loads({json.dumps(json.dumps(lines, ensure_ascii=False))}):\n"
        "    print(json.dumps(obj, ensure_ascii=False))\n"
        "    sys.stdout.flush()\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def _executor(
    db: AsyncSession,
    tmp_path: Path,
    lines: list[dict[str, Any]],
    *,
    exit_code: int = 0,
    sleep_before: float = 0.0,
    timeout_sec: float = 30.0,
    secret_values: frozenset[str] = frozenset(),
) -> ExternalAgentExecutor:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    argv = _agent_script(tmp_path, lines, exit_code=exit_code, sleep_before=sleep_before)
    return ExternalAgentExecutor(
        _ScriptAdapter(argv),
        LocalEnvironment(ws),
        TraceRecorder(db),
        workspace=ws,
        timeout_sec=timeout_sec,
        secret_values=secret_values,
    )


async def test_end_to_end_completed_run(db: AsyncSession, tmp_path: Path) -> None:
    executor = _executor(db, tmp_path, [_INIT, _ASSISTANT, _TOOL_RESULT, _RESULT])
    outcome = await executor.run("сделай hello.py", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    assert outcome.error is None
    assert outcome.final_answer == "готово: hello.py создан"
    assert outcome.tokens_used == 140
    assert outcome.cost_usd == 0.05
    assert outcome.iterations == 3

    run = (await db.execute(select(Run))).scalars().one()
    assert run.state == RunState.COMPLETED
    assert run.meta["executor"] == "external"
    assert run.meta["adapter"] == "claude-code"
    assert run.meta["agent_session_id"] == "sess-1"
    assert run.tokens_used == 140

    messages = (await db.execute(select(Message).order_by(Message.index_in_run))).scalars().all()
    # Финальный ответ дублируется assistant-сообщением — write-ahead повтор
    # делегации (ADR-0015 фаза 3) читает его через last_assistant_text.
    assert [m.role for m in messages] == ["user", "system", "assistant", "assistant"]
    assert messages[0].content == {"content": "сделай hello.py"}
    assert messages[2].content == {"content": "смотрю workspace"}
    assert messages[3].content == {"content": "готово: hello.py создан"}

    call = (await db.execute(select(ToolCall))).scalars().one()
    assert call.tool_name == "Bash"
    assert call.status == ToolCallStatus.SUCCEEDED
    assert call.policy_decision == "external"
    assert call.result == {"output": "hello.py"}


async def test_nonzero_exit_fails_and_closes_pending_calls(
    db: AsyncSession, tmp_path: Path
) -> None:
    executor = _executor(db, tmp_path, [_INIT, _ASSISTANT], exit_code=2)
    outcome = await executor.run("задача", AutonomyMode.YOLO)

    assert outcome.state is RunState.FAILED
    assert outcome.error is not None and "кодом 2" in outcome.error
    call = (await db.execute(select(ToolCall))).scalars().one()
    assert call.status == ToolCallStatus.FAILED
    assert call.error == "агент завершился до tool_result"


async def test_clean_exit_without_result_event_fails(db: AsyncSession, tmp_path: Path) -> None:
    executor = _executor(db, tmp_path, [_INIT])
    outcome = await executor.run("задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.FAILED
    assert outcome.error is not None and "без result-события" in outcome.error


async def test_error_result_fails_run(db: AsyncSession, tmp_path: Path) -> None:
    error_result = {
        **_RESULT,
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "лимит ходов",
    }
    executor = _executor(db, tmp_path, [_INIT, error_result])
    outcome = await executor.run("задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.FAILED
    assert outcome.error is not None and "агент сообщил ошибку" in outcome.error


async def test_wall_clock_timeout(db: AsyncSession, tmp_path: Path) -> None:
    executor = _executor(db, tmp_path, [_RESULT], sleep_before=5.0, timeout_sec=0.5)
    outcome = await executor.run("задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.FAILED
    assert outcome.error is not None and "wall-clock" in outcome.error


async def test_stream_is_redacted(db: AsyncSession, tmp_path: Path) -> None:
    leaked = {
        **_TOOL_RESULT,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "токен: super-sekret-value",
                    "is_error": False,
                }
            ],
        },
    }
    executor = _executor(
        db,
        tmp_path,
        [_INIT, _ASSISTANT, leaked, _RESULT],
        secret_values=frozenset({"super-sekret-value"}),
    )
    outcome = await executor.run("задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED
    call = (await db.execute(select(ToolCall))).scalars().one()
    assert "super-sekret-value" not in json.dumps(call.result)
    assert "[REDACTED]" in call.result["output"]


# --- Fail-closed гейт в TaskRunner ------------------------------------------


def _make_workspace(tmp_path: Path, *, extra_yaml: str = "") -> Path:
    ws = tmp_path / "gate-ws"
    ws.mkdir(exist_ok=True)
    db_path = tmp_path / "state" / "svarog.db"
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {db_path}\n" + extra_yaml,
        encoding="utf-8",
    )
    return ws


async def test_external_requires_docker_fail_closed(tmp_path: Path) -> None:
    ws = _make_workspace(
        tmp_path,
        extra_yaml="executor:\n  type: external\n  external:\n    image: img:1\n",
    )
    runner = TaskRunner(load_config(project_dir=ws), ws)
    with pytest.raises(SandboxError, match="external"):
        await runner.run_once("задача", AutonomyMode.YOLO, hooks=RunHooks())
