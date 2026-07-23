"""Тесты профиля Dream (блок C §6): реестр инструментов урезан структурно."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.schema import SvarogConfig
from svarog_harness.memory.curator import MemoryAuditReport, MemoryFinding
from svarog_harness.memory.dream import build_dream_task
from svarog_harness.runtime.orchestrator import RunProfile, TaskRunner
from svarog_harness.sandbox.local import LocalEnvironment
from svarog_harness.scheduler.store import JobStore
from svarog_harness.scheduler.system_jobs import DREAM_JOB_NAME, ensure_system_jobs
from svarog_harness.storage.db import create_engine, create_session_factory, init_db

# Инструменты, которых у Dream быть не должно. Проверяем поимённо, а не по
# количеству: иначе тест сломается от каждого нового инструмента в проекте.
FORBIDDEN = ("remember", "bash", "write_file", "edit_file", "spawn_child_run", "update_plan")


def _runner(tmp_path: Path) -> TaskRunner:
    cfg = SvarogConfig.model_validate(
        {
            "models": {
                "default": "main",
                "providers": {"main": {"base_url": "http://localhost", "model": "m"}},
            },
            "memory": {"path": str(tmp_path / "memory")},
            "storage": {"db_path": str(tmp_path / "svarog.sqlite3")},
        }
    )
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    return TaskRunner(cfg, tmp_path)


def _names(tmp_path: Path, profile: RunProfile) -> list[str]:
    runner = _runner(tmp_path)
    registry = runner._build_registry(
        LocalEnvironment(tmp_path),
        [],
        [],
        [],
        [],
        None,
        None,
        mem_dir=tmp_path / "memory",
        memory_proposal_sink=[],
        profile=profile,
    )
    return registry.names()


@pytest.fixture
def dream_registry_names(tmp_path: Path) -> list[str]:
    return _names(tmp_path, RunProfile.DREAM)


@pytest.fixture
def default_registry_names(tmp_path: Path) -> list[str]:
    return _names(tmp_path, RunProfile.DEFAULT)


def test_dream_registry_has_only_memory_tools(dream_registry_names: list[str]) -> None:
    assert "read_memory" in dream_registry_names
    assert "propose_memory_change" in dream_registry_names


def test_dream_registry_excludes_writing_tools(dream_registry_names: list[str]) -> None:
    """Dream читает содержимое из внешних источников — shell и запись ему не даём."""
    assert [name for name in FORBIDDEN if name in dream_registry_names] == []


def test_default_profile_keeps_remember(default_registry_names: list[str]) -> None:
    """Обычный run не теряет прямую запись: Flow A не меняется (ADR-0003)."""
    assert "remember" in default_registry_names
    assert "propose_memory_change" not in default_registry_names


def test_profiles_are_distinct() -> None:
    assert RunProfile.DEFAULT != RunProfile.DREAM


# --- текст задачи Dream (блок C §6) -----------------------------------------


def test_dream_task_lists_audit_findings() -> None:
    """Находки детерминированного аудита подаются как факты, а не как гипотезы."""
    report = MemoryAuditReport(
        findings=[
            MemoryFinding("orphan", "projects/ghost/", "папка проекта без overview.md"),
            MemoryFinding("empty", "junk.md", "пустой файл"),
        ]
    )
    task = build_dream_task(report)
    assert "projects/ghost/" in task
    assert "junk.md" in task
    assert "propose_memory_change" in task


def test_dream_task_without_findings_still_asks_for_semantic_pass() -> None:
    """Чистая структура — не повод пропускать поиск дублей и противоречий."""
    task = build_dream_task(MemoryAuditReport(findings=[]))
    assert "находок нет" in task
    assert "противореч" in task


# --- системная джоба под гейтом конфига (блок C §7) --------------------------


_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC).replace(tzinfo=None)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _ensure(db: AsyncSession, *, dream_enabled: bool) -> list[str]:
    return await ensure_system_jobs(
        JobStore(db),
        workspace="/tmp/ws",
        autonomy="yolo",
        config_digest="d1",
        now=_NOW,
        prune_interval_sec=86_400,
        dream_enabled=dream_enabled,
        dream_interval_sec=86_400,
    )


async def test_dream_job_not_created_when_disabled(db: AsyncSession) -> None:
    await _ensure(db, dream_enabled=False)
    names = {job.name for job in await JobStore(db).list_jobs()}
    assert DREAM_JOB_NAME not in names


async def test_dream_job_created_once_when_enabled(db: AsyncSession) -> None:
    await _ensure(db, dream_enabled=True)
    await _ensure(db, dream_enabled=True)
    jobs = [job for job in await JobStore(db).list_jobs() if job.name == DREAM_JOB_NAME]
    assert len(jobs) == 1
    assert jobs[0].protected is True


async def test_disabled_dream_job_stays_disabled_on_restart(db: AsyncSession) -> None:
    """Инвариант блока D: код не возвращает включённой джобу, выключенную человеком."""
    await _ensure(db, dream_enabled=True)
    store = JobStore(db)
    job = next(j for j in await store.list_jobs() if j.name == DREAM_JOB_NAME)
    await store.set_enabled(job, False)

    await _ensure(db, dream_enabled=True)

    job = next(j for j in await store.list_jobs() if j.name == DREAM_JOB_NAME)
    assert job.enabled is False
