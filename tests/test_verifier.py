"""Тесты детерминированного verifier (#20, §6.11)."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.schema import CheckSpec
from svarog_harness.sandbox.local import LocalEnvironment
from svarog_harness.skills import scan_skills
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import CheckResult, CheckStatus
from svarog_harness.trace.recorder import TraceRecorder
from svarog_harness.verifier import Verifier, skill_checks

_SKILL_WITH_CHECK = """\
---
name: checked-skill
description: Скилл со своей проверкой.
version: 0.1.0
risk: low
checks:
  - "test -f expected.txt"
---
# Body
"""


def _verifier(workspace: Path) -> Verifier:
    return Verifier(LocalEnvironment(workspace), workspace)


async def test_passing_and_failing_commands(tmp_path: Path) -> None:
    checks = [
        CheckSpec(name="ok", command="exit 0"),
        CheckSpec(name="bad", command="echo проблема >&2; exit 1"),
    ]
    outcomes = await _verifier(tmp_path).run(checks, secret_scan=False)
    by_name = {o.name: o for o in outcomes}
    assert by_name["ok"].status is CheckStatus.PASSED
    assert by_name["bad"].status is CheckStatus.FAILED
    assert "проблема" in by_name["bad"].output


async def test_secret_scan_flags_workspace_secret(tmp_path: Path) -> None:
    # Публичный AWS example — заведомо ненастоящий ключ.
    (tmp_path / "config.txt").write_text("key = AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")
    outcomes = await _verifier(tmp_path).run([], secret_scan=True)
    scan = next(o for o in outcomes if o.name == "secret-scan")
    assert scan.status is CheckStatus.FAILED
    assert "AKIA" not in scan.output  # redaction в отчёте


async def test_secret_scan_clean(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    outcomes = await _verifier(tmp_path).run([], secret_scan=True)
    scan = next(o for o in outcomes if o.name == "secret-scan")
    assert scan.status is CheckStatus.PASSED


def test_skill_checks_only_for_loaded(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "checked-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_WITH_CHECK, encoding="utf-8")
    skills = scan_skills([tmp_path / "skills"]).skills

    # Скилл не загружался — его checks не включаются.
    assert skill_checks(skills, loaded_names=set()) == []
    # Загружен — check добавлен.
    specs = skill_checks(skills, loaded_names={"checked-skill"})
    assert len(specs) == 1
    assert specs[0].command == "test -f expected.txt"


# --- запись CheckResult ---


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_recorder_logs_checks_and_counts_failures(db: AsyncSession) -> None:
    recorder = TraceRecorder(db)
    run = await recorder.start_run(task="t", autonomy="yolo", model="m")
    await recorder.log_check_result(run, name="tests", status=CheckStatus.PASSED, output="")
    await recorder.log_check_result(run, name="lint", status=CheckStatus.FAILED, output="E501")
    await recorder.log_check_result(run, name="broken", status=CheckStatus.ERROR, output="boom")

    rows = (await db.execute(select(CheckResult))).scalars().all()
    assert len(rows) == 3
    assert await recorder.failed_check_count(run.id) == 2  # FAILED + ERROR
