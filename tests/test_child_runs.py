"""Тесты child runs (ADR-0015 фаза 3): spawn_child_run, worktree, кламп бюджета."""

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import AutonomyMode
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.runtime import orchestrator
from svarog_harness.runtime.agents.claude_code import ClaudeCodeAdapter
from svarog_harness.runtime.orchestrator import RunHooks, TaskRunner
from svarog_harness.storage.models import Run, RunState, ToolCall
from svarog_harness.tools.base import ToolError
from svarog_harness.tools.child_tools import SpawnChildRunArgs
from svarog_harness.trace.recorder import TraceRecorder


class ScriptedProvider(ModelProvider):
    """Общий сценарий для родителя и ребёнка: ходы снимаются по очереди."""

    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)
        self.seen_tool_names: list[list[str]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.seen_tool_names.append([tool.name for tool in tools])
        return self.turns.pop(0)


def _patch_provider(
    monkeypatch: pytest.MonkeyPatch, turns: list[CompletionResult]
) -> ScriptedProvider:
    provider = ScriptedProvider(turns)
    monkeypatch.setattr(orchestrator, "default_provider", lambda models_cfg, store=None: provider)
    return provider


def _tool_turn(*calls: ToolCallRequest) -> CompletionResult:
    return CompletionResult(content="", tool_calls=calls, usage=Usage(10, 5))


def _final(text: str) -> CompletionResult:
    return CompletionResult(content=text, usage=Usage(10, 5), finish_reason="stop")


def _git(ws: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ws), *args], check=True, capture_output=True, text=True
    ).stdout


def _make_workspace(tmp_path: Path, *, git: bool = True, extra_yaml: str = "") -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
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
    (ws / "README.md").write_text("проект\n", encoding="utf-8")
    if git:
        _git(ws, "init", "-b", "main")
        _git(ws, "config", "user.email", "t@t")
        _git(ws, "config", "user.name", "t")
        _git(ws, "add", "-A")
        _git(ws, "commit", "-m", "init")
    return ws


async def test_spawn_child_run_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = _make_workspace(tmp_path)
    provider = _patch_provider(
        monkeypatch,
        [
            # Родитель: делегирует подзадачу ребёнку.
            _tool_turn(
                ToolCallRequest(
                    id="p1",
                    name="spawn_child_run",
                    arguments_json='{"task": "напиши файл result.txt"}',
                )
            ),
            # Ребёнок: пишет файл и отчитывается.
            _tool_turn(
                ToolCallRequest(
                    id="c1",
                    name="write_file",
                    arguments_json='{"path": "result.txt", "content": "42"}',
                )
            ),
            _final("ребёнок записал 42"),
            # Родитель: финальный ответ.
            _final("родитель готов"),
        ],
    )
    runner = TaskRunner(load_config(project_dir=ws), ws)
    outcome = await runner.run_once("делегируй", AutonomyMode.YOLO, hooks=RunHooks())
    assert outcome.state is RunState.COMPLETED
    assert outcome.final_answer == "родитель готов"

    async def fetch(db: AsyncSession) -> tuple[list[Run], list[ToolCall]]:
        runs = list((await db.execute(select(Run).order_by(Run.created_at))).scalars())
        calls = list((await db.execute(select(ToolCall))).scalars())
        return runs, calls

    runs, calls = await runner.with_db(fetch)
    parent = next(r for r in runs if r.parent_run_id is None)
    child = next(r for r in runs if r.parent_run_id is not None)
    # Ребёнок — обычный Run со ссылкой на родителя и своим worktree-workspace.
    assert child.parent_run_id == parent.id
    assert child.state is RunState.COMPLETED
    assert child.workspace is not None and ".worktrees" in child.workspace
    # Физический worktree после успеха убран, работа ребёнка — на его ветке.
    assert not Path(child.workspace).exists()
    branches = _git(ws, "branch", "--list", "svarog/child-*")
    assert "svarog/child-" in branches
    branch = branches.strip().lstrip("* ").strip()
    assert _git(ws, "show", f"{branch}:result.txt") == "42"
    # Результат ребёнка вернулся родителю через tool result.
    spawn_call = next(c for c in calls if c.tool_name == "spawn_child_run")
    assert "ребёнок записал 42" in spawn_call.result["output"]
    assert "svarog/child-" in spawn_call.result["output"]
    # Ребёнку spawn_child_run не выдаётся — глубина дерева ограничена 1 уровнем.
    parent_tools, child_tools = provider.seen_tool_names[0], provider.seen_tool_names[1]
    assert "spawn_child_run" in parent_tools
    assert "spawn_child_run" not in child_tools


async def test_spawn_is_idempotent_via_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повторный spawn той же подзадачи возвращает результат из trace (write-ahead)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = _make_workspace(tmp_path)
    runner = TaskRunner(load_config(project_dir=ws), ws)

    async def action(db: AsyncSession) -> str:
        recorder = TraceRecorder(db)
        parent = await recorder.start_run(
            task="родитель", autonomy="yolo", model="m", workspace=str(ws)
        )
        child = await recorder.start_run(
            task="подзадача",
            autonomy="yolo",
            model="m",
            workspace="/tmp/gone",
            parent_run_id=parent.id,
        )
        await recorder.add_message(
            child, "assistant", {"content": "готовый ответ из trace", "tool_calls": []}
        )
        await recorder.finish_run(child, RunState.COMPLETED)
        return await runner.spawn_child_run(
            recorder, parent, AutonomyMode.YOLO, SpawnChildRunArgs(task="подзадача"), RunHooks()
        )

    result = await runner.with_db(action)
    assert "уже выполнен" in result
    assert "готовый ответ из trace" in result


async def test_spawn_requires_git_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = _make_workspace(tmp_path, git=False)
    runner = TaskRunner(load_config(project_dir=ws), ws)

    async def action(db: AsyncSession) -> None:
        recorder = TraceRecorder(db)
        parent = await recorder.start_run(
            task="родитель", autonomy="yolo", model="m", workspace=str(ws)
        )
        with pytest.raises(ToolError, match="git-workspace"):
            await runner.spawn_child_run(
                recorder, parent, AutonomyMode.YOLO, SpawnChildRunArgs(task="x"), RunHooks()
            )

    await runner.with_db(action)


async def test_child_budget_clamped_down_and_worktree_kept_on_suspend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Запрошенный бюджет ребёнка клампится вниз к родительскому; suspended-ребёнок
    оставляет worktree для resume."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = _make_workspace(
        tmp_path,
        extra_yaml="runtime:\n  max_iterations: 3\n  refuel_after_iterations: 100\n",
    )
    _patch_provider(
        monkeypatch,
        [
            # Родитель просит ребёнку 100 итераций — родительский потолок 3.
            _tool_turn(
                ToolCallRequest(
                    id="p1",
                    name="spawn_child_run",
                    arguments_json='{"task": "много работы", "max_iterations": 100}',
                )
            ),
            # Ребёнок: три итерации с прогрессом — упирается в клампнутый лимит.
            _tool_turn(
                ToolCallRequest(
                    id="c1", name="write_file", arguments_json='{"path": "a.txt", "content": "a"}'
                )
            ),
            _tool_turn(
                ToolCallRequest(
                    id="c2", name="write_file", arguments_json='{"path": "b.txt", "content": "b"}'
                )
            ),
            _tool_turn(
                ToolCallRequest(
                    id="c3", name="write_file", arguments_json='{"path": "c.txt", "content": "c"}'
                )
            ),
            # Родитель: получает ошибку spawn и отчитывается.
            _final("ребёнок не уложился"),
        ],
    )
    runner = TaskRunner(load_config(project_dir=ws), ws)
    outcome = await runner.run_once("делегируй много", AutonomyMode.YOLO, hooks=RunHooks())
    assert outcome.state is RunState.COMPLETED

    async def fetch(db: AsyncSession) -> tuple[Run, ToolCall]:
        runs = list((await db.execute(select(Run))).scalars())
        child = next(r for r in runs if r.parent_run_id is not None)
        calls = list((await db.execute(select(ToolCall))).scalars())
        spawn_call = next(c for c in calls if c.tool_name == "spawn_child_run")
        return child, spawn_call

    child, spawn_call = await runner.with_db(fetch)
    # Кламп: ребёнок остановился на родительском потолке 3, а не на своих 100.
    assert child.state is RunState.SUSPENDED
    assert child.iterations == 3
    # Ошибка вернулась родителю честно, worktree сохранён для resume ребёнка.
    assert spawn_call.error is not None
    assert "suspended" in spawn_call.error
    assert "worktree сохранён" in spawn_call.error
    assert child.workspace is not None and Path(child.workspace).is_dir()


# --- Делегация внешнему агенту (ADR-0016 фаза 3.5) --------------------------


class _ScriptAgent(ClaudeCodeAdapter):
    """Парсер настоящий (claude-code); команда — локальный скрипт-агент."""

    def __init__(self, argv: list[str]) -> None:
        super().__init__()
        self._argv = argv

    def command(self, task: str, *, session: str | None = None) -> list[str]:
        return list(self._argv)


def _external_agent_script(tmp_path: Path) -> list[str]:
    """Скрипт-агент: создаёт файл в cwd (worktree ребёнка) и печатает стрим."""
    import json as _json
    import sys as _sys

    events = [
        {"type": "system", "subtype": "init", "session_id": "sess-d1"},
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "пишу файл"}]},
            "session_id": "sess-d1",
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "agent.txt создан внешним агентом",
            "num_turns": 2,
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 50, "output_tokens": 20},
            "session_id": "sess-d1",
        },
    ]
    script = tmp_path / "fake_external_agent.py"
    script.write_text(
        "import json, pathlib, sys\n"
        'pathlib.Path("agent.txt").write_text("от внешнего агента", encoding="utf-8")\n'
        f"for obj in json.loads({_json.dumps(_json.dumps(events, ensure_ascii=False))}):\n"
        "    print(json.dumps(obj, ensure_ascii=False))\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    return [_sys.executable, str(script)]


async def test_delegate_child_to_external_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Нативный цикл — оркестратор, внешний агент — исполнитель подзадачи."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = _make_workspace(tmp_path, extra_yaml="executor:\n  external:\n    image: img:1\n")
    _patch_provider(
        monkeypatch,
        [
            _tool_turn(
                ToolCallRequest(
                    id="p1",
                    name="spawn_child_run",
                    arguments_json='{"task": "сделай agent.txt", "executor": "external"}',
                )
            ),
            _final("родитель готов"),
        ],
    )
    monkeypatch.setattr(
        orchestrator, "adapter_for", lambda cfg: _ScriptAgent(_external_agent_script(tmp_path))
    )
    # Fail-closed гейт docker-only тестируется отдельно; здесь исполняем локально.
    monkeypatch.setattr(TaskRunner, "assert_sandbox_available", lambda self: None)
    runner = TaskRunner(load_config(project_dir=ws), ws)
    outcome = await runner.run_once("делегируй", AutonomyMode.YOLO, hooks=RunHooks())
    assert outcome.state is RunState.COMPLETED
    assert outcome.final_answer == "родитель готов"

    async def fetch(db: AsyncSession) -> tuple[list[Run], list[ToolCall]]:
        runs = list((await db.execute(select(Run).order_by(Run.created_at))).scalars())
        calls = list((await db.execute(select(ToolCall))).scalars())
        return runs, calls

    runs, calls = await runner.with_db(fetch)
    parent = next(r for r in runs if r.parent_run_id is None)
    child = next(r for r in runs if r.parent_run_id is not None)
    # Ребёнок — внешний run со ссылкой на родителя, trace един.
    assert child.parent_run_id == parent.id
    assert child.state is RunState.COMPLETED
    assert child.meta["executor"] == "external"
    assert child.meta["adapter"] == "claude-code"
    assert child.meta["agent_session_id"] == "sess-d1"
    assert child.tokens_used == 70
    # Работа агента — на ветке ребёнка (host-flow commit), worktree убран.
    assert child.workspace is not None and not Path(child.workspace).exists()
    branches = _git(ws, "branch", "--list", "svarog/child-*")
    branch = branches.strip().lstrip("* ").strip()
    assert _git(ws, "show", f"{branch}:agent.txt") == "от внешнего агента"
    # Результат ребёнка вернулся родителю tool-результатом.
    spawn_call = next(c for c in calls if c.tool_name == "spawn_child_run")
    assert "agent.txt создан внешним агентом" in spawn_call.result["output"]


async def test_delegate_requires_external_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без executor.external делегация — tool-ошибка, родитель продолжает."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = _make_workspace(tmp_path)
    _patch_provider(
        monkeypatch,
        [
            _tool_turn(
                ToolCallRequest(
                    id="p1",
                    name="spawn_child_run",
                    arguments_json='{"task": "подзадача", "executor": "external"}',
                )
            ),
            _final("сделаю сам"),
        ],
    )
    runner = TaskRunner(load_config(project_dir=ws), ws)
    outcome = await runner.run_once("делегируй", AutonomyMode.YOLO, hooks=RunHooks())
    assert outcome.state is RunState.COMPLETED

    async def fetch(db: AsyncSession) -> ToolCall:
        calls = list((await db.execute(select(ToolCall))).scalars())
        return next(c for c in calls if c.tool_name == "spawn_child_run")

    spawn_call = await runner.with_db(fetch)
    assert spawn_call.error is not None
    assert "executor.external" in spawn_call.error
    # Worktree не создавался — проверка конфига идёт до него.
    assert not (ws.parent / ".worktrees").exists()


async def test_delegate_fail_closed_without_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """local-trusted + делегация: fail-closed гейт возвращается tool-ошибкой,
    свежесозданный worktree убирается."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = _make_workspace(tmp_path, extra_yaml="executor:\n  external:\n    image: img:1\n")
    _patch_provider(
        monkeypatch,
        [
            _tool_turn(
                ToolCallRequest(
                    id="p1",
                    name="spawn_child_run",
                    arguments_json='{"task": "подзадача", "executor": "external"}',
                )
            ),
            _final("сделаю сам"),
        ],
    )
    runner = TaskRunner(load_config(project_dir=ws), ws)
    outcome = await runner.run_once("делегируй", AutonomyMode.YOLO, hooks=RunHooks())
    assert outcome.state is RunState.COMPLETED

    async def fetch(db: AsyncSession) -> ToolCall:
        calls = list((await db.execute(select(ToolCall))).scalars())
        return next(c for c in calls if c.tool_name == "spawn_child_run")

    spawn_call = await runner.with_db(fetch)
    assert spawn_call.error is not None
    assert "docker" in spawn_call.error
    leftovers = (
        list((ws.parent / ".worktrees").glob("*")) if (ws.parent / ".worktrees").exists() else []
    )
    assert leftovers == []
