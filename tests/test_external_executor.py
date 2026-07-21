"""Тесты внешнего executor'а (ADR-0016 фаза 1): адаптер claude-code,
стрим → trace, redaction, fail-closed гейты."""

import asyncio
import json
import subprocess
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
from svarog_harness.runtime.executor import AgentLaunch
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
    argv = adapter.command(AgentLaunch(task="сделай hello.py"))
    assert argv[:3] == ["claude", "-p", "сделай hello.py"]
    assert "stream-json" in argv
    assert "bypassPermissions" in argv  # tier 1: границу держит sandbox
    assert "--resume" not in argv
    resumed = adapter.command(AgentLaunch(task="t", session="sess-9"))
    assert resumed[-2:] == ["--resume", "sess-9"]
    with_mcp = adapter.command(AgentLaunch(task="t", mcp_config="/run/svarog/mcp.json"))
    assert "--mcp-config" in with_mcp and "--strict-mcp-config" in with_mcp


# --- Конфигурация -----------------------------------------------------------


def test_external_type_requires_section() -> None:
    with pytest.raises(ValidationError, match=r"executor\.external"):
        ExecutorConfig(type="external")


def test_subscription_requires_oauth_ref() -> None:
    with pytest.raises(ValidationError, match="oauth_token_ref"):
        ExternalExecutorConfig(image="img:1", auth="subscription")


def test_subscription_only_claude_code() -> None:
    with pytest.raises(ValidationError, match="claude-code"):
        ExternalExecutorConfig(
            image="img:1", adapter="codex", auth="subscription", oauth_token_ref="TOK"
        )


def test_subscription_valid_config() -> None:
    cfg = ExternalExecutorConfig(image="img:1", auth="subscription", oauth_token_ref="CLAUDE_OAUTH")
    assert cfg.auth == "subscription"
    assert cfg.api_key_ref is None


def test_adapter_registry() -> None:
    adapter = adapter_for(ExternalExecutorConfig(image="img:1"))
    assert adapter.name == "claude-code"


def test_config_digest_covers_executor(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    native = load_config(project_dir=ws, user_config_path=tmp_path / "no-user.yaml")
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
        self.launches: list[AgentLaunch] = []

    def command(self, launch: AgentLaunch) -> list[str]:
        self.launches.append(launch)
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


async def test_reasoning_fallback_when_no_text_events(db: AsyncSession, tmp_path: Path) -> None:
    """gpt-oss/harmony: ответ уходит только в reasoning-канал (content пуст) —
    финал берётся из последнего reasoning, а не теряется как «(без ответа)»."""
    from svarog_harness.runtime.agents.opencode import OpencodeAdapter

    class _OpencodeScriptAdapter(OpencodeAdapter):
        def __init__(self, argv: list[str]) -> None:
            super().__init__()
            self._argv = argv

        def command(self, launch: AgentLaunch) -> list[str]:
            return list(self._argv)

    stream: list[dict[str, Any]] = [
        {
            "type": "step_start",
            "sessionID": "ses_1",
            "part": {"id": "prt_1", "type": "step-start"},
        },
        {"type": "reasoning", "sessionID": "ses_1", "part": {"text": "прикидываю план"}},
        {
            "type": "reasoning",
            "sessionID": "ses_1",
            "part": {"text": "В workspace только index.html — заготовка веб-страницы."},
        },
        {
            "type": "step_finish",
            "sessionID": "ses_1",
            "reason": "stop",
            "cost": 0,
            "tokens": {"input": 10, "output": 1},
        },
    ]
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    executor = ExternalAgentExecutor(
        _OpencodeScriptAdapter(_agent_script(tmp_path, stream)),
        LocalEnvironment(ws),
        TraceRecorder(db),
        workspace=ws,
        timeout_sec=30.0,
    )
    outcome = await executor.run("что в проекте", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED
    assert outcome.error is None
    assert outcome.final_answer == "В workspace только index.html — заготовка веб-страницы."
    messages = (await db.execute(select(Message).order_by(Message.index_in_run))).scalars().all()
    # Reasoning в trace не пишется — только финал (фолбэк) assistant-сообщением.
    assistant = [m for m in messages if m.role == "assistant"]
    assert [m.content["content"] for m in assistant] == [outcome.final_answer]


async def test_text_event_wins_over_reasoning_fallback(db: AsyncSession, tmp_path: Path) -> None:
    """Обычный прогон: есть text-реплика — reasoning финал не подменяет."""
    executor = _executor(db, tmp_path, [_INIT, _ASSISTANT, _TOOL_RESULT, {**_RESULT, "result": ""}])
    outcome = await executor.run("задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED
    assert outcome.final_answer == "смотрю workspace"


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
    runner = TaskRunner(load_config(project_dir=ws, user_config_path=tmp_path / "no-user.yaml"), ws)
    with pytest.raises(SandboxError, match="external"):
        await runner.run_once("задача", AutonomyMode.YOLO, hooks=RunHooks())


# --- Suspend / resume / бюджет (ADR-0016 §3/§7) -------------------------------


class _FakeSuspend:
    """Минимальный SuspendSignal для теста без полного BridgeControl."""

    def __init__(self) -> None:
        self.suspend = asyncio.Event()
        self.suspend_reason = "approval abc12345: ждём решения человека"


async def test_suspend_signal_interrupts_agent(db: AsyncSession, tmp_path: Path) -> None:
    """Suspend от control-plane отменяет стрим и переводит run в waiting_approval."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    # Агент печатает init и «зависает» на 30 секунд.
    script = tmp_path / "hanging_agent.py"
    script.write_text(
        "import json, sys, time\n"
        f"print({json.dumps(json.dumps(_INIT, ensure_ascii=False))})\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    signal = _FakeSuspend()
    executor = ExternalAgentExecutor(
        _ScriptAdapter([sys.executable, str(script)]),
        LocalEnvironment(ws),
        TraceRecorder(db),
        workspace=ws,
        timeout_sec=60.0,
        suspend_signal=signal,
    )

    async def fire_suspend() -> None:
        await asyncio.sleep(0.3)
        signal.suspend.set()

    fire = asyncio.create_task(fire_suspend())
    started = asyncio.get_running_loop().time()
    outcome = await executor.run("задача", AutonomyMode.YOLO)
    await fire
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 10  # агент убит, 30-секундный sleep не дожидались
    assert outcome.state is RunState.WAITING_APPROVAL
    assert outcome.error is not None and "ждём решения" in outcome.error
    run = (await db.execute(select(Run))).scalars().one()
    assert run.state == RunState.WAITING_APPROVAL
    assert run.finished_at is None  # нетерминальный переход (ADR-0005)


async def test_budget_exceeded_suspends_run(db: AsyncSession, tmp_path: Path) -> None:
    """Флаг бюджета с прокси переводит run в suspended, usage — с прокси."""
    from svarog_harness.runtime.bridge import BridgeBudget, BridgeUsage, RunBridge, UpstreamConfig

    bridge = RunBridge(
        upstream=UpstreamConfig(base_url="http://unused", api_key=None),
        budget=BridgeBudget(max_tokens=100, max_cost_usd=1.0),
        loop=asyncio.get_running_loop(),
    )
    bridge.usage = BridgeUsage(input_tokens=90, output_tokens=30, requests=2, budget_exceeded=True)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    argv = _agent_script(tmp_path, [_INIT, _RESULT])
    executor = ExternalAgentExecutor(
        _ScriptAdapter(argv),
        LocalEnvironment(ws),
        TraceRecorder(db),
        workspace=ws,
        timeout_sec=30.0,
        bridge=bridge,
    )
    outcome = await executor.run("задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.SUSPENDED
    assert outcome.error is not None and "бюджет" in outcome.error
    # Источник истины usage — прокси, не stream-события агента (§3).
    assert outcome.tokens_used == 120
    run = (await db.execute(select(Run))).scalars().one()
    assert run.finished_at is None


async def test_resume_continues_agent_session(db: AsyncSession, tmp_path: Path) -> None:
    """resume поднимает сессию агента --resume и дописывает тот же run."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    recorder = TraceRecorder(db)
    adapter = _ScriptAdapter(_agent_script(tmp_path, [_INIT, _RESULT]))
    executor = ExternalAgentExecutor(
        adapter,
        LocalEnvironment(ws),
        recorder,
        workspace=ws,
        timeout_sec=30.0,
    )
    run = await recorder.start_run(
        task="задача", autonomy="yolo", model="external:claude-code", workspace=str(ws)
    )
    await recorder.merge_run_meta(
        run, {"executor": "external", "adapter": "claude-code", "agent_session_id": "sess-old"}
    )
    await recorder.set_run_state(run, RunState.WAITING_APPROVAL, error="ждём")

    outcome = await executor.resume(run, "Approval получен — продолжай", agent_session="sess-old")
    assert outcome.state is RunState.COMPLETED
    assert outcome.run_id == run.id  # та же Run-запись, без нового run
    # Сессия агента передана в --resume.
    assert adapter.launches[-1].session == "sess-old"
    messages = (await db.execute(select(Message).order_by(Message.index_in_run))).scalars().all()
    assert any("Approval получен" in str(m.content) for m in messages if m.role == "user")


async def test_supervised_requires_cooperative_tier(tmp_path: Path) -> None:
    """Supervised с containment-tier — fail-closed отказ (ADR-0016 §6)."""
    ws = _make_workspace(
        tmp_path,
        extra_yaml=("executor:\n  type: external\n  external:\n    image: img:1\n"),
    )
    runner = TaskRunner(load_config(project_dir=ws, user_config_path=tmp_path / "no-user.yaml"), ws)
    with pytest.raises(SandboxError, match="cooperative"):
        runner.assert_external_autonomy_supported(AutonomyMode.SUPERVISED)
    # cooperative + claude-code (hooks) — допустим.
    ws2 = tmp_path / "gate-ws2"
    ws2.mkdir()
    (ws2 / "svarog.yaml").write_text(
        (ws / "svarog.yaml")
        .read_text(encoding="utf-8")
        .replace("    image: img:1\n", "    image: img:1\n    enforcement: cooperative\n"),
        encoding="utf-8",
    )
    runner2 = TaskRunner(
        load_config(project_dir=ws2, user_config_path=tmp_path / "no-user.yaml"), ws2
    )
    runner2.assert_external_autonomy_supported(AutonomyMode.SUPERVISED)
    # cooperative, но адаптер без hooks (codex) — отказ.
    ws3 = tmp_path / "gate-ws3"
    ws3.mkdir()
    (ws3 / "svarog.yaml").write_text(
        (ws2 / "svarog.yaml")
        .read_text(encoding="utf-8")
        .replace("    image: img:1\n", "    image: img:1\n    adapter: codex\n"),
        encoding="utf-8",
    )
    runner3 = TaskRunner(
        load_config(project_dir=ws3, user_config_path=tmp_path / "no-user.yaml"), ws3
    )
    with pytest.raises(SandboxError, match="hook"):
        runner3.assert_external_autonomy_supported(AutonomyMode.SUPERVISED)


async def test_autonomy_gate_fires_before_task_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отказ гейта автономии не должен оставлять мусорную task-ветку (S15a,
    кампания 21.07.2026: workspace оставался на svarog/*, а checkout master
    терял svarog.yaml)."""
    ws = _make_workspace(
        tmp_path,
        extra_yaml=(
            "executor:\n  type: external\n  external:\n    image: img:1\n"
            "    adapter: opencode\n"
            "    base_url: https://openrouter.ai/api\n"
            f"skills:\n  paths: ['{tmp_path / 'skills'}']\n"
        ),
    )
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.name=T", "-c", "user.email=t@localhost", "commit", "-qm", "init"],
    ):
        subprocess.run(argv, cwd=ws, check=True)

    runner = TaskRunner(load_config(project_dir=ws, user_config_path=tmp_path / "no-user.yaml"), ws)
    # Сужаем тест до порядка гейтов: sandbox-проверка не должна требовать docker.
    monkeypatch.setattr(TaskRunner, "assert_sandbox_available", lambda self: None)
    with pytest.raises(SandboxError, match="cooperative"):
        await runner.run_once("задача", AutonomyMode.SUPERVISED, hooks=RunHooks())
    branches = subprocess.run(
        ["git", "branch", "--list", "svarog/*"], cwd=ws, capture_output=True, text=True
    ).stdout.strip()
    assert branches == ""
