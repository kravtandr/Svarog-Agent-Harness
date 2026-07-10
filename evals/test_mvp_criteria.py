"""Eval-сценарии критериев готовности MVP (§26, ADR-0008).

Каждый сценарий — исполняемая проверка одного критерия из §26 на настоящем
стеке Svarog с scripted-LLM (без сети). Запускаются в CI отдельным шагом.
"""

from pathlib import Path

from sqlalchemy import select

from evals._harness import EvalHarness, ScriptedProvider, call, final, make_harness
from svarog_harness.config.schema import AutonomyMode, PoliciesConfig, RuntimeConfig
from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.policy.engine import PolicyAction, PolicyEngine
from svarog_harness.scaffold import scaffold_agent_home
from svarog_harness.skills import scan_skills, skill_cards
from svarog_harness.storage.models import Approval, Checkpoint, Message, RunState, ToolCall
from svarog_harness.trace.recorder import TraceRecorder


async def test_criterion_init_agent_home(tmp_path: Path) -> None:
    """§26: создать agent-home Git repo с обнаруживаемыми скиллами."""
    scaffold_agent_home(tmp_path)
    repo = GitRepo(tmp_path / "memory")
    await repo.init()
    assert (tmp_path / "svarog.yaml").exists()
    scan = scan_skills([tmp_path / "skills"])
    # §26: обнаружить скиллы из skills/ и передать краткие карточки.
    assert scan.errors == []
    assert scan.skills
    assert "read_skill" in skill_cards(scan.skills)


async def test_criterion_run_task_with_files(tmp_path: Path) -> None:
    """§26: выполнить задачу с чтением/записью файлов + сохранить trace + commit."""
    harness = make_harness(
        tmp_path,
        [
            call("write_file", '{"path": "hello.py", "content": "print(42)\\n"}'),
            final("Файл hello.py создан."),
        ],
    )
    # workspace — git-репозиторий (Flow C).
    repo = GitRepo(harness.workspace)
    await repo.init()
    await repo.ensure_identity()
    (harness.workspace / "README.md").write_text("# ws\n", encoding="utf-8")
    await repo.add_all()
    await repo.commit("seed")

    outcome = await harness.run("создай hello.py")
    assert outcome.state is RunState.COMPLETED
    assert (harness.workspace / "hello.py").read_text(encoding="utf-8") == "print(42)\n"

    # Trace полон: сообщения, tool call, checkpoint'ы.
    async with harness.session() as db:
        messages = (await db.execute(select(Message))).scalars().all()
        tool_calls = (await db.execute(select(ToolCall))).scalars().all()
        checkpoints = (await db.execute(select(Checkpoint))).scalars().all()
    roles = [m.role for m in messages]
    assert roles[0] == "system"
    assert "assistant" in roles and "tool" in roles
    assert [t.tool_name for t in tool_calls] == ["write_file"]
    assert len(checkpoints) >= 2  # write-ahead + после исполнения


async def test_criterion_bash_in_sandbox(tmp_path: Path) -> None:
    """§26: выполнить shell-команду в sandbox (здесь — local backend для CI)."""
    harness = make_harness(
        tmp_path,
        [
            call("bash", '{"command": "echo из-песочницы > out.txt"}'),
            final("Команда выполнена."),
        ],
    )
    outcome = await harness.run("запусти echo")
    assert outcome.state is RunState.COMPLETED
    assert (harness.workspace / "out.txt").read_text(encoding="utf-8").strip() == "из-песочницы"


async def test_criterion_approval_gate(tmp_path: Path) -> None:
    """§26: применить policies и approval — push в protected требует approval."""
    engine = PolicyEngine(autonomy=AutonomyMode.YOLO, policies=PoliciesConfig(), workspace=tmp_path)
    decision = engine.evaluate_action("git.push", {"branch": "main"})
    assert decision.action is PolicyAction.REQUIRE_APPROVAL
    assert decision.action_type == "git.push_protected"


async def test_criterion_request_approval_flow(tmp_path: Path) -> None:
    """§26: approval приостанавливает run (waiting_approval), решение продолжает."""
    harness = make_harness(
        tmp_path,
        [
            call("request_approval", '{"action": "рискованный шаг", "details": "detail"}'),
            final("Продолжаю после approval."),
        ],
    )
    outcome = await harness.run("сделай рискованное")
    assert outcome.state is RunState.WAITING_APPROVAL

    async with harness.session() as db:
        recorder = TraceRecorder(db)
        approval = (await db.execute(select(Approval))).scalar_one()
        await recorder.decide_approval(approval, approved=True, decided_by="eval")

    resumed = await harness.resume(outcome.run_id)
    assert resumed.state is RunState.COMPLETED


async def test_criterion_refuel_long_task(tmp_path: Path) -> None:
    """§26: продолжить долгую задачу через refuel loop (task_state.md)."""
    harness = make_harness(
        tmp_path,
        [
            call("list_dir", "{}", call_id="c0"),
            call("list_dir", "{}", call_id="c1"),
            final("Готово."),
        ],
        runtime=RuntimeConfig(max_iterations=6, refuel_after_iterations=1),
    )
    outcome = await harness.run("длинная задача")
    assert outcome.state is RunState.SUSPENDED
    assert outcome.error is not None and "refuel" in outcome.error
    assert (harness.workspace / "task_state.md").exists()
    assert "# Task state" in (harness.workspace / "task_state.md").read_text(encoding="utf-8")

    for _ in range(5):
        outcome = await harness.resume(outcome.run_id)
        if outcome.state is RunState.COMPLETED:
            break

    assert outcome.state is RunState.COMPLETED
    assert (harness.workspace / "task_state.md").exists()
    assert "# Task state" in (harness.workspace / "task_state.md").read_text(encoding="utf-8")


async def test_criterion_resume_after_crash(tmp_path: Path) -> None:
    """§26/ADR-0008: resume после «падения процесса» — из checkpoint без потери прогресса."""
    # Первый «процесс»: доходит до лимита итераций и suspended.
    first = EvalHarness(
        workspace=(tmp_path / "ws"),
        db_path=tmp_path / "state" / "svarog.db",
        provider=ScriptedProvider(
            [call("list_dir", "{}", call_id="a0"), call("list_dir", "{}", call_id="a1")]
        ),
        runtime=RuntimeConfig(max_iterations=2, refuel_after_iterations=1),
    )
    first.workspace.mkdir(parents=True, exist_ok=True)
    suspended = await first.run("работа на два процесса")
    assert suspended.state is RunState.SUSPENDED

    # Второй «процесс»: та же БД и workspace, новый provider — resume завершает.
    second = EvalHarness(
        workspace=first.workspace,
        db_path=first.db_path,
        provider=ScriptedProvider([final("Завершено после рестарта.")]),
        runtime=RuntimeConfig(max_iterations=10, refuel_after_iterations=5),
    )
    resumed = await second.resume(suspended.run_id)
    assert resumed.state is RunState.COMPLETED
    assert resumed.final_answer == "Завершено после рестарта."
