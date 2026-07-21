"""Тесты Skill Curator слой 1 (#27): lifecycle-переходы, pin, provenance, реактивация."""

import json
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import AutonomyMode, CuratorConfig
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.runtime import orchestrator
from svarog_harness.runtime.orchestrator import RunHooks, TaskRunner
from svarog_harness.scheduler.store import JobStore
from svarog_harness.skills.curator import CuratorStore, prune_layer1
from svarog_harness.skills.loader import scan_skills
from svarog_harness.skills.models import Skill
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import (
    JobOrigin,
    ScheduleKind,
    SkillLifecycleStatus,
    utcnow,
)
from svarog_harness.trace.recorder import TraceRecorder
from svarog_harness.trace.viewer import fetch_run

_CFG = CuratorConfig(stale_after_days=30, archive_after_days=90)


def _skill_md(name: str, provenance: str = "agent") -> str:
    return (
        f"---\nname: {name}\ndescription: Тестовый скилл.\nversion: 0.1.0\n"
        f"risk: low\nprovenance: {provenance}\n---\n# When to use\nиногда.\n"
    )


def _make_skills(root: Path, names_provenance: dict[str, str]) -> list[Skill]:
    for name, provenance in names_provenance.items():
        skill_dir = root / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_skill_md(name, provenance), encoding="utf-8")
    return scan_skills([root]).skills


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_unused_skill_archived_after_threshold(db: AsyncSession, tmp_path: Path) -> None:
    skills = _make_skills(tmp_path / "skills", {"greeter": "agent"})
    later = utcnow() + timedelta(days=100)
    transitions = await prune_layer1(db, skills, _CFG, now=later)
    assert [(t.skill_name, t.new) for t in transitions] == [
        ("greeter", SkillLifecycleStatus.ARCHIVED)
    ]
    assert await CuratorStore(db).archived_names() == {"greeter"}


async def test_unused_skill_stale_before_archive(db: AsyncSession, tmp_path: Path) -> None:
    skills = _make_skills(tmp_path / "skills", {"greeter": "agent"})
    transitions = await prune_layer1(db, skills, _CFG, now=utcnow() + timedelta(days=40))
    assert transitions[0].new is SkillLifecycleStatus.STALE


async def test_fresh_skill_stays_active(db: AsyncSession, tmp_path: Path) -> None:
    skills = _make_skills(tmp_path / "skills", {"greeter": "agent"})
    transitions = await prune_layer1(db, skills, _CFG, now=utcnow() + timedelta(days=5))
    assert transitions == []


async def test_pinned_skill_exempt(db: AsyncSession, tmp_path: Path) -> None:
    skills = _make_skills(tmp_path / "skills", {"greeter": "agent"})
    await CuratorStore(db).set_pinned("greeter", True)
    transitions = await prune_layer1(db, skills, _CFG, now=utcnow() + timedelta(days=100))
    assert transitions == []
    assert await CuratorStore(db).archived_names() == set()


async def test_non_agent_skill_untouched(db: AsyncSession, tmp_path: Path) -> None:
    skills = _make_skills(tmp_path / "skills", {"official-one": "official"})
    transitions = await prune_layer1(db, skills, _CFG, now=utcnow() + timedelta(days=100))
    assert transitions == []
    assert await CuratorStore(db).get("official-one") is None


async def test_reactivation_on_recent_use(db: AsyncSession, tmp_path: Path) -> None:
    skills = _make_skills(tmp_path / "skills", {"greeter": "agent"})
    await prune_layer1(db, skills, _CFG, now=utcnow() + timedelta(days=100))
    assert await CuratorStore(db).archived_names() == {"greeter"}

    # Скилл снова загрузили — свежий SkillLoad возвращает его в active.
    run = await TraceRecorder(db).start_run(task="t", autonomy="yolo", model="test")
    await TraceRecorder(db).log_skill_load(run, skill_name="greeter", skill_version="0.1.0")
    transitions = await prune_layer1(db, skills, _CFG, now=utcnow())
    assert transitions[0].new is SkillLifecycleStatus.ACTIVE
    assert await CuratorStore(db).archived_names() == set()


# --- интеграция: archived-скилл исключён из run ---


def _write_config(ws: Path, tmp_path: Path) -> None:
    (ws / "svarog.yaml").write_text(
        "models:\n  default: local\n  providers:\n    local:\n"
        "      base_url: http://localhost:9/v1\n      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )


def _patch_provider(monkeypatch: pytest.MonkeyPatch, turns: list[CompletionResult]) -> None:
    class ScriptedProvider(ModelProvider):
        def __init__(self) -> None:
            self.turns = list(turns)

        async def complete(
            self,
            messages: list[ChatMessage],
            tools: list[ToolDefinition],
            *,
            on_text_delta: Callable[[str], None] | None = None,
        ) -> CompletionResult:
            return self.turns.pop(0)

    provider = ScriptedProvider()
    monkeypatch.setattr(orchestrator, "default_provider", lambda models_cfg, store=None: provider)


async def test_archived_skill_excluded_from_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    # helper остаётся активным (pinned), greeter уходит в archived.
    _make_skills(ws / "skills", {"greeter": "agent", "helper": "agent"})

    runner = TaskRunner(load_config(project_dir=ws), ws)

    async def archive(db: AsyncSession) -> None:
        await CuratorStore(db).set_pinned("helper", True)
        skills = scan_skills([ws / "skills"]).skills
        await prune_layer1(db, skills, _CFG, now=utcnow() + timedelta(days=100))
        assert await CuratorStore(db).archived_names() == {"greeter"}

    await runner.with_db(archive)

    args = json.dumps({"name": "greeter"})
    _patch_provider(
        monkeypatch,
        [
            CompletionResult(
                content="",
                tool_calls=(ToolCallRequest(id="c1", name="read_skill", arguments_json=args),),
                usage=Usage(10, 5),
            ),
            CompletionResult(content="ок", usage=Usage(10, 5)),
        ],
    )
    outcome = await runner.run_once("используй greeter", AutonomyMode.YOLO, hooks=RunHooks())

    async def tool_messages(db: AsyncSession) -> str:
        _, messages, _, _ = await fetch_run(db, outcome.run_id)
        return "\n".join(str(m.content.get("content", "")) for m in messages if m.role == "tool")

    text = await runner.with_db(tool_messages)
    assert "не найден" in text  # archived-скилл недоступен через read_skill


# --- Блок D §8: скиллы, используемые джобами планировщика --------------------


async def test_skill_used_by_enabled_job_is_not_archived(db: AsyncSession, tmp_path: Path) -> None:
    """Скилл, загружавшийся в run'е включённой джобы, не архивируется.

    Иначе редко срабатывающая автоматизация теряла бы свой скилл — точка
    расширения, помеченная в pruning.py до появления планировщика.
    """
    skills = _make_skills(tmp_path / "skills", {"greeter": "agent"})
    recorder = TraceRecorder(db)
    store = JobStore(db)
    job = await store.create(
        name="ночная",
        kind=ScheduleKind.EVERY,
        spec="3600",
        tz="UTC",
        task="поздоровайся",
        workspace=str(tmp_path),
        autonomy="supervised",
        config_digest="d",
        origin=JobOrigin.HUMAN,
        first_run_at=utcnow(),
    )
    await store.set_enabled(job, True)

    run = await recorder.start_run(task="поздоровайся", autonomy="supervised", model="m")
    await recorder.merge_run_meta(run, {"cron_job_id": job.id})
    await recorder.log_skill_load(run, skill_name="greeter", skill_version=None)

    transitions = await prune_layer1(db, skills, _CFG, now=utcnow() + timedelta(days=100))
    assert transitions == []
    assert await CuratorStore(db).archived_names() == set()


async def test_skill_used_by_disabled_job_is_archived(db: AsyncSession, tmp_path: Path) -> None:
    """Выключенная джоба скилл не защищает: автоматизации больше нет."""
    skills = _make_skills(tmp_path / "skills", {"greeter": "agent"})
    recorder = TraceRecorder(db)
    store = JobStore(db)
    job = await store.create(
        name="выключенная",
        kind=ScheduleKind.EVERY,
        spec="3600",
        tz="UTC",
        task="поздоровайся",
        workspace=str(tmp_path),
        autonomy="supervised",
        config_digest="d",
        origin=JobOrigin.HUMAN,
        first_run_at=utcnow(),
    )

    run = await recorder.start_run(task="поздоровайся", autonomy="supervised", model="m")
    await recorder.merge_run_meta(run, {"cron_job_id": job.id})
    await recorder.log_skill_load(run, skill_name="greeter", skill_version=None)

    transitions = await prune_layer1(db, skills, _CFG, now=utcnow() + timedelta(days=100))
    assert [(t.skill_name, t.new) for t in transitions] == [
        ("greeter", SkillLifecycleStatus.ARCHIVED)
    ]
