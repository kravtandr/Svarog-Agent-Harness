# Блок D: планировщик — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать проекту источник запуска run'ов, отличный от человека, не размывая заморозку прав.

**Architecture:** Новый пакет `scheduler/` из четырёх узких модулей: чистый расчёт расписания, хранилище джоб с атомарным захватом, тик и регистрация системных джоб. Исполнение задачи инжектируется колбэком, поэтому `scheduler` не импортирует `runtime` — связывает их CLI. Демон `svarog scheduler` — отдельный процесс; `serve` джобы не исполняет.

**Tech Stack:** Python 3.12+, uv, Pydantic v2, SQLAlchemy async + alembic, Typer, pytest (`asyncio_mode=auto`), ruff, mypy. Новых внешних зависимостей не добавляется.

**Спек:** `docs/superpowers/specs/2026-07-21-scheduler-design.md`

## Global Constraints

- Комментарии, docstring'и, сообщения об ошибках и вывод CLI — на русском; код и идентификаторы — на английском.
- Conventional Commits, заголовок ≤72 символов, на английском в императиве. Scope — модуль (`scheduler`, `storage`, `policy`, `cli`, `skills`, `docs`).
- Перед каждым коммитом: `uv run ruff check`, `uv run ruff format`, `uv run mypy`, `uv run pytest` — всё зелёное.
- Известное пред-существующее падение: `tests/test_external_docker.py::test_external_run_once_in_docker` (загрязнение docker-окружения). Не чинить, отмечать в отчёте.
- Набор тестов флейкает: полный прогон может дать одно падение в случайном тесте, в изоляции проходящем. Упавший тест перезапустить изолированно; если проходит — отметить и продолжать.
- Правила зависимостей (`docs/repo-structure.md`): `cli` → `runtime` → компоненты → `storage`/`trace`. **Пакет `scheduler` не импортирует `runtime`**: исполнение задачи приходит колбэком, который подставляет CLI.
- Никаких новых внешних зависимостей: полный cron-синтаксис вне объёма (спек §2).
- Текущий момент везде передаётся параметром, а не читается из глобального модуля — иначе тесты придётся писать через подмену времени.
- Никакого мёртвого кода; секретов в коде и тестах нет.

---

## File Structure

**Создаётся:**
- `src/svarog_harness/scheduler/__init__.py` — публичный фасад пакета.
- `src/svarog_harness/scheduler/schedule.py` — чистый расчёт следующего срабатывания. Зависит только от stdlib.
- `src/svarog_harness/scheduler/store.py` — доступ к таблице джоб, атомарный захват. Зависит от `storage`.
- `src/svarog_harness/scheduler/ticker.py` — один проход планировщика. Исполнение задачи — инжектируемый колбэк.
- `src/svarog_harness/scheduler/system_jobs.py` — регистрация защищённых системных джоб.
- `src/svarog_harness/storage/migrations/versions/f6b8d3e2a9c4_add_cron_jobs.py` — миграция.
- `src/svarog_harness/tools/schedule_tools.py` — агентский инструмент.
- `tests/test_schedule_calc.py`, `tests/test_scheduler_store.py`, `tests/test_scheduler_tick.py`, `tests/test_schedule_tool.py`.

**Модифицируется:**
- `src/svarog_harness/storage/models.py` — модель `CronJob`.
- `src/svarog_harness/config/schema.py` — `SchedulerConfig`.
- `src/svarog_harness/policy/engine.py` — `schedule.create` в `CRITICAL_ACTIONS`.
- `src/svarog_harness/cli/main.py` — команды `scheduler` и группа `cron`.
- `src/svarog_harness/runtime/orchestrator.py` — регистрация инструмента.
- `src/svarog_harness/skills/curator/pruning.py` — защита скиллов, использованных джобами.
- Документация (Task 8).

---

## Task 1: Модель, миграция и конфиг

**Files:**
- Modify: `src/svarog_harness/storage/models.py`
- Create: `src/svarog_harness/storage/migrations/versions/f6b8d3e2a9c4_add_cron_jobs.py`
- Modify: `src/svarog_harness/config/schema.py`
- Test: `tests/test_scheduler_store.py`

**Interfaces:**
- Consumes: ничего.
- Produces: модель `CronJob` (таблица `cron_jobs`); перечисления `ScheduleKind` (`every`, `daily_at`), `JobOrigin` (`human`, `agent`, `system`); `SchedulerConfig.interval_sec: int = 30`, поле `SvarogConfig.scheduler`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_scheduler_store.py`:

```python
"""Хранилище джоб планировщика (блок D §1)."""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import CronJob, JobOrigin, ScheduleKind


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_cron_job_persists_frozen_rights(db: AsyncSession, tmp_path: Path) -> None:
    """Права джобы (автономия и дайджест конфига) хранятся вместе с ней."""
    due = datetime(2026, 7, 21, 3, 0, tzinfo=timezone.utc)
    job = CronJob(
        name="ночная сводка",
        schedule_kind=ScheduleKind.DAILY_AT,
        schedule_spec="03:00",
        tz="UTC",
        task="собери сводку",
        workspace=str(tmp_path),
        autonomy="supervised",
        config_digest="deadbeef",
        origin=JobOrigin.HUMAN,
        enabled=True,
        next_run_at=due,
    )
    db.add(job)
    await db.commit()

    stored = (await db.execute(select(CronJob))).scalar_one()
    assert stored.autonomy == "supervised"
    assert stored.config_digest == "deadbeef"
    assert stored.origin is JobOrigin.HUMAN
    assert stored.protected is False
    assert stored.run_count == 0


async def test_cron_job_defaults_are_safe(db: AsyncSession, tmp_path: Path) -> None:
    """Джоба по умолчанию выключена и не защищена: активацию делает явный шаг."""
    job = CronJob(
        name="черновик",
        schedule_kind=ScheduleKind.EVERY,
        schedule_spec="3600",
        tz="UTC",
        task="задача",
        workspace=str(tmp_path),
        autonomy="supervised",
        config_digest="d",
        origin=JobOrigin.AGENT,
        next_run_at=datetime(2026, 7, 21, tzinfo=timezone.utc) + timedelta(hours=1),
    )
    db.add(job)
    await db.commit()

    stored = (await db.execute(select(CronJob))).scalar_one()
    assert stored.enabled is False
    assert stored.protected is False
    assert stored.last_status is None
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_scheduler_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'CronJob' from 'svarog_harness.storage.models'`

- [ ] **Step 3: Добавить модель**

В `src/svarog_harness/storage/models.py` рядом с другими перечислениями добавить:

```python
class ScheduleKind(StrEnum):
    """Вид расписания (блок D §2). Полный cron-синтаксис вне объёма."""

    EVERY = "every"  # интервал в секундах, schedule_spec — число
    DAILY_AT = "daily_at"  # время суток, schedule_spec — "HH:MM"


class JobOrigin(StrEnum):
    """Кто завёл джобу: от этого зависит, можно ли её менять агенту."""

    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"
```

И модель рядом с `SkillLoad`:

```python
class CronJob(TimestampedBase):
    """Джоба планировщика: источник запуска run'ов, отличный от человека (ADR-0019).

    Права (`autonomy`, `config_digest`) заморожены при создании или approval:
    последующее ослабление конфига не повышает прав уже одобренной джобы, а
    расхождение дайджеста при срабатывании отключает джобу (fail-closed).
    """

    __tablename__ = "cron_jobs"

    name: Mapped[str] = mapped_column(String(128))
    schedule_kind: Mapped[ScheduleKind] = mapped_column(_enum(ScheduleKind))
    schedule_spec: Mapped[str] = mapped_column(String(64))
    tz: Mapped[str] = mapped_column(String(64), default="UTC")
    task: Mapped[str] = mapped_column(Text)
    workspace: Mapped[str] = mapped_column(String(1024))
    session_id: Mapped[str | None] = mapped_column(String(36))
    # Замороженные права (§6): режим автономии и снимок security-конфига.
    autonomy: Mapped[str] = mapped_column(String(32))
    config_digest: Mapped[str] = mapped_column(String(64))
    origin: Mapped[JobOrigin] = mapped_column(_enum(JobOrigin))
    # Выключена по умолчанию: активацию делает явный шаг (approval или CLI).
    enabled: Mapped[bool] = mapped_column(default=False, index=True)
    # Системные джобы агентский инструмент не меняет и не удаляет.
    protected: Mapped[bool] = mapped_column(default=False)
    next_run_at: Mapped[datetime] = mapped_column(index=True)
    last_run_at: Mapped[datetime | None]
    last_status: Mapped[str | None] = mapped_column(String(64))
    run_count: Mapped[int] = mapped_column(default=0)
```

- [ ] **Step 4: Написать миграцию**

Создать `src/svarog_harness/storage/migrations/versions/f6b8d3e2a9c4_add_cron_jobs.py`:

```python
"""add cron_jobs (ADR-0019, планировщик)

Revision ID: f6b8d3e2a9c4
Revises: e5a9c4d1b8f3
Create Date: 2026-07-21 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6b8d3e2a9c4"
down_revision: str | None = "e5a9c4d1b8f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cron_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("schedule_kind", sa.String(length=32), nullable=False),
        sa.Column("schedule_spec", sa.String(length=64), nullable=False),
        sa.Column("tz", sa.String(length=64), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("workspace", sa.String(length=1024), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("autonomy", sa.String(length=32), nullable=False),
        sa.Column("config_digest", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("protected", sa.Boolean(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(length=64), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False),
    )
    op.create_index("ix_cron_jobs_enabled", "cron_jobs", ["enabled"], unique=False)
    op.create_index("ix_cron_jobs_next_run_at", "cron_jobs", ["next_run_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_cron_jobs_next_run_at", table_name="cron_jobs")
    op.drop_index("ix_cron_jobs_enabled", table_name="cron_jobs")
    op.drop_table("cron_jobs")
```

Реализующему: сверь типы колонок `id`/`created_at` с тем, как их объявляет `TimestampedBase` в `storage/models.py`, и приведи миграцию к фактическому виду — расхождение схемы с моделью поймает `svarog doctor`, но лучше не создавать его вовсе.

- [ ] **Step 5: Добавить конфиг**

В `src/svarog_harness/config/schema.py` рядом с `SupervisorConfig` добавить:

```python
class SchedulerConfig(StrictModel):
    """Демон расписания `svarog scheduler` (ADR-0019).

    Отдельный процесс: `serve` джобы НЕ исполняет. Джоба, чей workspace занят
    интерактивной работой, пропускает тик и пробует на следующем.
    """

    interval_sec: int = Field(default=30, gt=0)
```

И поле в `SvarogConfig` рядом с `supervisor`:

```python
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
```

Реализующему: найди класс `SvarogConfig` и добавь поле тем же способом, каким там объявлены соседние секции.

- [ ] **Step 6: Запустить тесты**

Run: `uv run pytest tests/test_scheduler_store.py tests/test_config.py tests/test_cli_doctor.py -v`
Expected: PASS. `doctor` сверяет alembic head со схемой — если он ругается, миграция расходится с моделью.

- [ ] **Step 7: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/storage/models.py src/svarog_harness/storage/migrations src/svarog_harness/config/schema.py tests/test_scheduler_store.py
git commit -m "feat(storage): add cron_jobs table and scheduler config"
```

---

## Task 2: Расчёт расписания

**Files:**
- Create: `src/svarog_harness/scheduler/__init__.py`
- Create: `src/svarog_harness/scheduler/schedule.py`
- Create: `tests/test_schedule_calc.py`
- Test: `tests/test_schedule_calc.py`

**Interfaces:**
- Consumes: `ScheduleKind` (Task 1).
- Produces: `next_run_after(kind: ScheduleKind, spec: str, tz: str, now: datetime) -> datetime`; `ScheduleSpecError(ValueError)`; `parse_spec(kind: ScheduleKind, spec: str) -> None` (валидация без вычисления).

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_schedule_calc.py`:

```python
"""Расчёт следующего срабатывания (блок D §2). Время — параметр, не глобальное."""

from datetime import datetime, timezone

import pytest

from svarog_harness.scheduler.schedule import ScheduleSpecError, next_run_after, parse_spec
from svarog_harness.storage.models import ScheduleKind


def test_every_adds_interval() -> None:
    now = datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)
    assert next_run_after(ScheduleKind.EVERY, "3600", "UTC", now) == datetime(
        2026, 7, 21, 11, 0, tzinfo=timezone.utc
    )


def test_daily_at_picks_today_when_time_still_ahead() -> None:
    now = datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc)
    assert next_run_after(ScheduleKind.DAILY_AT, "03:00", "UTC", now) == datetime(
        2026, 7, 21, 3, 0, tzinfo=timezone.utc
    )


def test_daily_at_rolls_over_midnight_when_time_passed() -> None:
    """Время уже прошло сегодня — срабатывание переносится на завтра."""
    now = datetime(2026, 7, 21, 5, 0, tzinfo=timezone.utc)
    assert next_run_after(ScheduleKind.DAILY_AT, "03:00", "UTC", now) == datetime(
        2026, 7, 22, 3, 0, tzinfo=timezone.utc
    )


def test_daily_at_respects_timezone() -> None:
    """03:00 в Москве — это 00:00 UTC того же дня."""
    now = datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc)
    result = next_run_after(ScheduleKind.DAILY_AT, "03:00", "Europe/Moscow", now)
    assert result == datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)


def test_daily_at_exactly_now_moves_to_next_day() -> None:
    """Граница: ровно время срабатывания — следующее уже завтра, без петли."""
    now = datetime(2026, 7, 21, 3, 0, tzinfo=timezone.utc)
    assert next_run_after(ScheduleKind.DAILY_AT, "03:00", "UTC", now) == datetime(
        2026, 7, 22, 3, 0, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("spec", ["0", "-5", "не число", ""])
def test_every_rejects_bad_spec(spec: str) -> None:
    with pytest.raises(ScheduleSpecError):
        parse_spec(ScheduleKind.EVERY, spec)


@pytest.mark.parametrize("spec", ["25:00", "03:99", "3:00pm", "", "0300"])
def test_daily_at_rejects_bad_spec(spec: str) -> None:
    with pytest.raises(ScheduleSpecError):
        parse_spec(ScheduleKind.DAILY_AT, spec)


def test_unknown_timezone_is_rejected() -> None:
    now = datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc)
    with pytest.raises(ScheduleSpecError):
        next_run_after(ScheduleKind.DAILY_AT, "03:00", "Нигде/Такого", now)
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_schedule_calc.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.scheduler'`

- [ ] **Step 3: Написать модуль**

Создать `src/svarog_harness/scheduler/__init__.py`:

```python
"""Планировщик: источник запуска run'ов, отличный от человека (ADR-0019)."""
```

Создать `src/svarog_harness/scheduler/schedule.py`:

```python
"""Расчёт следующего срабатывания джобы (блок D §2).

Чистые функции: текущий момент приходит параметром, к БД и глобальному времени
модуль не обращается — это делает расчёт тестируемым без подмены времени.

Поддержаны два вида расписания; полный cron-синтаксис вне объёма, он потребовал
бы внешней зависимости.
"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from svarog_harness.storage.models import ScheduleKind

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ScheduleSpecError(ValueError):
    """Некорректные параметры расписания."""


def parse_spec(kind: ScheduleKind, spec: str) -> None:
    """Проверить параметры расписания; ScheduleSpecError — если они негодные."""
    if kind is ScheduleKind.EVERY:
        try:
            seconds = int(spec)
        except ValueError:
            raise ScheduleSpecError(f"интервал должен быть числом секунд, получено {spec!r}") from None
        if seconds <= 0:
            raise ScheduleSpecError(f"интервал должен быть больше нуля, получено {seconds}")
        return
    if _HHMM_RE.match(spec) is None:
        raise ScheduleSpecError(f"время должно быть в формате HH:MM, получено {spec!r}")


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleSpecError(f"неизвестная таймзона: {tz!r}") from None


def next_run_after(kind: ScheduleKind, spec: str, tz: str, now: datetime) -> datetime:
    """Ближайшее срабатывание строго ПОСЛЕ `now`.

    Строгое «после» важно на границе: при расчёте от момента самого
    срабатывания следующее должно уехать на сутки вперёд, иначе джоба
    зациклилась бы на одном и том же моменте.
    """
    parse_spec(kind, spec)
    zone = _zone(tz)
    if kind is ScheduleKind.EVERY:
        return now + timedelta(seconds=int(spec))

    hour, minute = (int(part) for part in spec.split(":"))
    local = now.astimezone(zone)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(now.tzinfo)
```

- [ ] **Step 4: Запустить тест**

Run: `uv run pytest tests/test_schedule_calc.py -v`
Expected: PASS

- [ ] **Step 5: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/scheduler tests/test_schedule_calc.py
git commit -m "feat(scheduler): add schedule calculation"
```

---

## Task 3: Хранилище джоб и атомарный захват

**Files:**
- Create: `src/svarog_harness/scheduler/store.py`
- Test: `tests/test_scheduler_store.py`

**Interfaces:**
- Consumes: `CronJob`, `ScheduleKind`, `JobOrigin` (Task 1); `next_run_after` (Task 2).
- Produces: класс `JobStore(db: AsyncSession)` с методами:
  - `async create(...) -> CronJob`
  - `async list_jobs(*, only_enabled: bool = False) -> list[CronJob]`
  - `async get(job_id: str) -> CronJob` (`JobNotFoundError` при отсутствии)
  - `async claim_due(now: datetime) -> list[CronJob]` — атомарный захват
  - `async finish(job: CronJob, *, status: str, now: datetime) -> None`
  - `async set_enabled(job: CronJob, enabled: bool) -> None`
  - `async disable_with_reason(job: CronJob, reason: str) -> None`
  - `async remove(job: CronJob) -> None`
  - исключения `JobNotFoundError`, `ProtectedJobError`.

- [ ] **Step 1: Написать падающий тест**

Дописать в `tests/test_scheduler_store.py`:

```python
async def _make_job(db: AsyncSession, tmp_path: Path, *, due: datetime, enabled: bool = True):
    from svarog_harness.scheduler.store import JobStore

    store = JobStore(db)
    job = await store.create(
        name="джоба",
        kind=ScheduleKind.EVERY,
        spec="3600",
        tz="UTC",
        task="задача",
        workspace=str(tmp_path),
        autonomy="supervised",
        config_digest="d",
        origin=JobOrigin.HUMAN,
        first_run_at=due,
    )
    if enabled:
        await store.set_enabled(job, True)
    return store, job


async def test_claim_due_returns_only_enabled_and_due(db: AsyncSession, tmp_path: Path) -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    store, due_job = await _make_job(db, tmp_path, due=now - timedelta(minutes=1))
    await _make_job(db, tmp_path, due=now + timedelta(hours=1))  # ещё не время
    await _make_job(db, tmp_path, due=now - timedelta(hours=1), enabled=False)  # выключена

    claimed = await store.claim_due(now)
    assert [job.id for job in claimed] == [due_job.id]


async def test_claim_due_is_single_flight(db: AsyncSession, tmp_path: Path) -> None:
    """Второй захват той же джобы в тот же момент ничего не возвращает.

    Это рубеж против двух одновременно работающих демонов (§3).
    """
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    store, _ = await _make_job(db, tmp_path, due=now - timedelta(minutes=1))

    first = await store.claim_due(now)
    second = await store.claim_due(now)

    assert len(first) == 1
    assert second == []


async def test_claim_moves_next_run_forward_without_catchup(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Демон простоял сутки — джоба срабатывает один раз, без догоняющего шторма."""
    long_ago = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    store, job = await _make_job(db, tmp_path, due=long_ago)

    claimed = await store.claim_due(now)
    assert len(claimed) == 1
    assert job.next_run_at > now
    assert await store.claim_due(now) == []


async def test_disable_with_reason_stops_job(db: AsyncSession, tmp_path: Path) -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    store, job = await _make_job(db, tmp_path, due=now - timedelta(minutes=1))

    await store.disable_with_reason(job, "конфиг изменился")

    assert job.enabled is False
    assert job.last_status is not None
    assert "конфиг изменился" in job.last_status
    assert await store.claim_due(now) == []


async def test_protected_job_cannot_be_removed(db: AsyncSession, tmp_path: Path) -> None:
    from svarog_harness.scheduler.store import JobStore, ProtectedJobError

    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    store = JobStore(db)
    job = await store.create(
        name="системная",
        kind=ScheduleKind.EVERY,
        spec="3600",
        tz="UTC",
        task="курирование",
        workspace=str(tmp_path),
        autonomy="supervised",
        config_digest="d",
        origin=JobOrigin.SYSTEM,
        first_run_at=now,
        protected=True,
    )
    with pytest.raises(ProtectedJobError):
        await store.remove(job)
```

Реализующему: добавь недостающие импорты (`timedelta`) в начало файла, а не внутрь тестов.

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_scheduler_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.scheduler.store'`

- [ ] **Step 3: Написать модуль**

Создать `src/svarog_harness/scheduler/store.py`:

```python
"""Хранилище джоб планировщика с атомарным захватом (блок D §1, §3).

Захват реализован как compare-and-set по `next_run_at` внутри транзакции: это
рубеж против двух одновременно работающих демонов, не полагающийся на
внешний лок.
"""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.scheduler.schedule import next_run_after, parse_spec
from svarog_harness.storage.models import CronJob, JobOrigin, ScheduleKind


class JobNotFoundError(Exception):
    """Джоба с таким идентификатором не найдена."""


class ProtectedJobError(Exception):
    """Системная джоба не изменяется и не удаляется извне."""


class JobStore:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(
        self,
        *,
        name: str,
        kind: ScheduleKind,
        spec: str,
        tz: str,
        task: str,
        workspace: str,
        autonomy: str,
        config_digest: str,
        origin: JobOrigin,
        first_run_at: datetime,
        session_id: str | None = None,
        protected: bool = False,
    ) -> CronJob:
        """Завести джобу. Создаётся выключенной: активация — отдельный шаг."""
        parse_spec(kind, spec)
        job = CronJob(
            name=name,
            schedule_kind=kind,
            schedule_spec=spec,
            tz=tz,
            task=task,
            workspace=workspace,
            session_id=session_id,
            autonomy=autonomy,
            config_digest=config_digest,
            origin=origin,
            enabled=False,
            protected=protected,
            next_run_at=first_run_at,
        )
        self._db.add(job)
        await self._db.commit()
        return job

    async def list_jobs(self, *, only_enabled: bool = False) -> list[CronJob]:
        stmt = select(CronJob).order_by(CronJob.next_run_at)
        if only_enabled:
            stmt = stmt.where(CronJob.enabled.is_(True))
        return list((await self._db.execute(stmt)).scalars())

    async def get(self, job_id: str) -> CronJob:
        job = await self._db.get(CronJob, job_id)
        if job is None:
            raise JobNotFoundError(f"джоба не найдена: {job_id}")
        return job

    async def claim_due(self, now: datetime) -> list[CronJob]:
        """Захватить джобы, чьё время пришло, сдвинув их расписание вперёд.

        Сдвиг считается от `now`, а не от просроченного `next_run_at`: демон,
        простоявший сутки, отрабатывает джобу ОДИН раз, без догоняющего шторма
        (at-least-once без catch-up, §5).
        """
        stmt = select(CronJob).where(
            CronJob.enabled.is_(True), CronJob.next_run_at <= now
        )
        candidates = list((await self._db.execute(stmt)).scalars())

        claimed: list[CronJob] = []
        for job in candidates:
            following = next_run_after(job.schedule_kind, job.schedule_spec, job.tz, now)
            # compare-and-set: захватывает тот, кто первым сдвинул next_run_at.
            result = await self._db.execute(
                update(CronJob)
                .where(CronJob.id == job.id, CronJob.next_run_at == job.next_run_at)
                .values(next_run_at=following)
            )
            if result.rowcount:
                await self._db.refresh(job)
                claimed.append(job)
        await self._db.commit()
        return claimed

    async def finish(self, job: CronJob, *, status: str, now: datetime) -> None:
        """Записать исход срабатывания."""
        job.last_run_at = now
        job.last_status = status
        job.run_count += 1
        await self._db.commit()

    async def set_enabled(self, job: CronJob, enabled: bool) -> None:
        job.enabled = enabled
        await self._db.commit()

    async def disable_with_reason(self, job: CronJob, reason: str) -> None:
        """Отключить джобу и записать причину — путь fail-closed (§6)."""
        job.enabled = False
        job.last_status = f"отключена: {reason}"
        await self._db.commit()

    async def remove(self, job: CronJob) -> None:
        if job.protected:
            raise ProtectedJobError(f"системная джоба не удаляется: {job.name}")
        await self._db.delete(job)
        await self._db.commit()
```

- [ ] **Step 4: Запустить тест**

Run: `uv run pytest tests/test_scheduler_store.py -v`
Expected: PASS

- [ ] **Step 5: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/scheduler/store.py tests/test_scheduler_store.py
git commit -m "feat(scheduler): add job store with atomic claim"
```

---

## Task 4: Тик планировщика

**Files:**
- Create: `src/svarog_harness/scheduler/ticker.py`
- Create: `tests/test_scheduler_tick.py`
- Test: `tests/test_scheduler_tick.py`

**Interfaces:**
- Consumes: `JobStore` (Task 3), `CronJob` (Task 1).
- Produces: `@dataclass(frozen=True) JobRunRequest(job_id: str, task: str, workspace: str, autonomy: str)`; `RunJob = Callable[[JobRunRequest], Awaitable[str]]` — колбэк исполнения, возвращает статус; `async tick(store: JobStore, *, now: datetime, current_digest: str, run_job: RunJob, workspace_busy: Callable[[str], Awaitable[bool]]) -> list[str]` — возвращает идентификаторы отработавших джоб.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_scheduler_tick.py`:

```python
"""Один проход планировщика (блок D §3-§6)."""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.scheduler.store import JobStore
from svarog_harness.scheduler.ticker import JobRunRequest, tick
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import JobOrigin, ScheduleKind

_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _enabled_job(db: AsyncSession, tmp_path: Path, *, digest: str = "d"):
    store = JobStore(db)
    job = await store.create(
        name="джоба",
        kind=ScheduleKind.EVERY,
        spec="3600",
        tz="UTC",
        task="задача",
        workspace=str(tmp_path),
        autonomy="supervised",
        config_digest=digest,
        origin=JobOrigin.HUMAN,
        first_run_at=_NOW - timedelta(minutes=1),
    )
    await store.set_enabled(job, True)
    return store, job


async def _never_busy(workspace: str) -> bool:
    return False


async def test_tick_runs_due_job(db: AsyncSession, tmp_path: Path) -> None:
    store, job = await _enabled_job(db, tmp_path)
    seen: list[JobRunRequest] = []

    async def run_job(request: JobRunRequest) -> str:
        seen.append(request)
        return "completed"

    done = await tick(
        store, now=_NOW, current_digest="d", run_job=run_job, workspace_busy=_never_busy
    )

    assert done == [job.id]
    assert len(seen) == 1
    assert seen[0].task == "задача"
    assert seen[0].autonomy == "supervised"
    assert job.run_count == 1
    assert job.last_status == "completed"


async def test_tick_skips_busy_workspace_without_losing_schedule(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Занятый workspace — не ошибка: пропускаем тик, расписание не теряем."""
    store, job = await _enabled_job(db, tmp_path)
    calls: list[JobRunRequest] = []

    async def run_job(request: JobRunRequest) -> str:
        calls.append(request)
        return "completed"

    async def always_busy(workspace: str) -> bool:
        return True

    done = await tick(
        store, now=_NOW, current_digest="d", run_job=run_job, workspace_busy=always_busy
    )

    assert done == []
    assert calls == []
    assert job.enabled is True
    assert job.last_status is not None and "занят" in job.last_status


async def test_tick_disables_job_on_config_drift(db: AsyncSession, tmp_path: Path) -> None:
    """Дайджест конфига разошёлся — джоба отключается, run НЕ создаётся."""
    store, job = await _enabled_job(db, tmp_path, digest="старый")
    calls: list[JobRunRequest] = []

    async def run_job(request: JobRunRequest) -> str:
        calls.append(request)
        return "completed"

    done = await tick(
        store, now=_NOW, current_digest="новый", run_job=run_job, workspace_busy=_never_busy
    )

    assert done == []
    assert calls == []
    assert job.enabled is False
    assert job.last_status is not None and "конфиг" in job.last_status


async def test_tick_records_failure_without_disabling(db: AsyncSession, tmp_path: Path) -> None:
    """Упавшая задача не выключает джобу: расписание продолжает работать."""
    store, job = await _enabled_job(db, tmp_path)

    async def run_job(request: JobRunRequest) -> str:
        raise RuntimeError("провал")

    done = await tick(
        store, now=_NOW, current_digest="d", run_job=run_job, workspace_busy=_never_busy
    )

    assert done == []
    assert job.enabled is True
    assert job.last_status is not None and "ошибка" in job.last_status
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_scheduler_tick.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.scheduler.ticker'`

- [ ] **Step 3: Написать модуль**

Создать `src/svarog_harness/scheduler/ticker.py`:

```python
"""Один проход планировщика (блок D §3-§6).

Исполнение задачи приходит колбэком: пакет `scheduler` не импортирует
`runtime`, связывает их CLI. Текущий момент и дайджест конфига — параметры, а
не глобальное состояние.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from svarog_harness.scheduler.store import JobStore


@dataclass(frozen=True)
class JobRunRequest:
    """Что именно исполнить по сработавшей джобе."""

    job_id: str
    task: str
    workspace: str
    autonomy: str


RunJob = Callable[[JobRunRequest], Awaitable[str]]
WorkspaceBusy = Callable[[str], Awaitable[bool]]


async def tick(
    store: JobStore,
    *,
    now: datetime,
    current_digest: str,
    run_job: RunJob,
    workspace_busy: WorkspaceBusy,
) -> list[str]:
    """Отработать джобы, чьё время пришло. Возвращает id отработавших.

    Порядок проверок важен: сначала заморозка прав (§6), потом занятость
    workspace (§4). Джоба с разошедшимся конфигом не должна исполняться даже
    на свободном рабочем дереве.
    """
    done: list[str] = []
    for job in await store.claim_due(now):
        if job.config_digest != current_digest:
            # Fail-closed, как resume при config drift (ADR-0015 §0.4):
            # ослабление конфига не повышает прав уже одобренной джобы.
            await store.disable_with_reason(job, "конфиг изменился с момента одобрения")
            continue
        if await workspace_busy(job.workspace):
            # Не ошибка: workspace занят интерактивной работой. Расписание уже
            # сдвинуто захватом, поэтому джоба вернётся на следующем тике.
            await store.finish(job, status="пропущено: workspace занят", now=now)
            continue
        request = JobRunRequest(
            job_id=job.id, task=job.task, workspace=job.workspace, autonomy=job.autonomy
        )
        try:
            status = await run_job(request)
        except Exception as exc:  # noqa: BLE001 — исход джобы, а не отказ планировщика
            # Упавшая задача не выключает джобу: расписание продолжает работать,
            # а причина видна в last_status и в trace самого run'а.
            await store.finish(job, status=f"ошибка: {type(exc).__name__}", now=now)
            continue
        await store.finish(job, status=status, now=now)
        done.append(job.id)
    return done
```

- [ ] **Step 4: Запустить тест**

Run: `uv run pytest tests/test_scheduler_tick.py -v`
Expected: PASS

- [ ] **Step 5: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/scheduler/ticker.py tests/test_scheduler_tick.py
git commit -m "feat(scheduler): add tick with frozen rights and busy skip"
```

---

## Task 5: Демон и CLI

**Files:**
- Modify: `src/svarog_harness/cli/main.py`
- Test: `tests/test_cli_scheduler.py` (создать)

**Interfaces:**
- Consumes: `JobStore`, `tick`, `JobRunRequest` (Tasks 3-4); `TaskRunner`, `config_digest` (существуют).
- Produces: команда `svarog scheduler`; группа команд `svarog cron add|list|show|enable|disable|remove|run-now`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_cli_scheduler.py` со сценариями: `cron add` заводит выключенную джобу; `cron list` её показывает; `cron enable` включает; `cron remove` удаляет; `cron remove` для системной джобы завершается ненулевым кодом с внятным сообщением.

Реализующему: возьми за образец структуру `tests/test_cli.py` — там уже есть `runner = CliRunner()`, фикстура `workspace` и способ подсунуть конфиг; используй их, а не заводи свои. Каждый тест утверждает и код возврата, и наличие ключевой строки в выводе.

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_cli_scheduler.py -v`
Expected: FAIL — команды `cron` нет, Typer возвращает ненулевой код с сообщением о неизвестной команде

- [ ] **Step 3: Добавить группу команд `cron`**

В `src/svarog_harness/cli/main.py` рядом с другими под-приложениями объявить:

```python
cron_app = typer.Typer(help="Джобы планировщика (ADR-0019).", no_args_is_help=True)
app.add_typer(cron_app, name="cron")
```

Команды: `add` (параметры `--every` либо `--at`, `--task`, `--workspace`, `--tz`, режим автономии теми же флагами, что у `run`), `list`, `show`, `enable`, `disable`, `remove`, `run-now`.

`add` замораживает права: `autonomy` из флагов или конфига, `config_digest` — тем же вызовом, каким его снимает старт run'а (`runtime.config_snapshot.config_digest`). Джоба создаётся выключенной; `list` показывает это отдельной колонкой, чтобы не было иллюзии, что она уже работает.

`remove` для защищённой джобы ловит `ProtectedJobError` и печатает причину, завершаясь ненулевым кодом.

`run-now` исполняет задачу немедленно и **не** сдвигает `next_run_at`: это проверка задачи, а не внеочередное срабатывание.

- [ ] **Step 4: Добавить команду `scheduler`**

Команда `svarog scheduler` — цикл с интервалом `cfg.scheduler.interval_sec`:

```python
@app.command()
def scheduler(
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Рабочая директория (по умолчанию cwd)"),
    ] = None,
) -> None:
    """Демон расписания: исполняет джобы (ADR-0019).

    Отдельный процесс: `svarog serve` джобы НЕ исполняет.
    """
```

Внутри: бесконечный цикл `tick(...)` с ожиданием `cfg.scheduler.interval_sec` между проходами. Колбэк `run_job` создаёт `TaskRunner` для workspace джобы и вызывает `run_once(task, autonomy)`, возвращая `outcome.state.value` как статус. Колбэк `workspace_busy` опирается на существующую проверку живого lease по workspace — реализующему: найди, как `acquire_workspace_lease` определяет занятость (`trace/recorder.py`), и используй ту же проверку, не дублируя её логику.

Весь тик оборачивается существующим межпроцессным локом (`storage/locks.py`) — первый рубеж против двух демонов; второй рубеж (compare-and-set) уже внутри `claim_due`.

`Run.meta["cron_job_id"]` проставляется при создании run'а по джобе — реализующему: найди, как `TaskRunner` принимает метаданные run'а, и передай идентификатор джобы туда; если такого канала нет, допиши его минимально, не расширяя сигнатуру за пределы одного необязательного параметра.

- [ ] **Step 5: Запустить тесты**

Run: `uv run pytest tests/test_cli_scheduler.py tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 6: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/cli/main.py tests/test_cli_scheduler.py
git commit -m "feat(cli): add scheduler daemon and cron commands"
```

---

## Task 6: Агентский инструмент и critical-набор

**Files:**
- Create: `src/svarog_harness/tools/schedule_tools.py`
- Modify: `src/svarog_harness/policy/engine.py:29`
- Modify: `src/svarog_harness/runtime/orchestrator.py`
- Create: `tests/test_schedule_tool.py`
- Test: `tests/test_schedule_tool.py`, `tests/test_policy.py`

**Interfaces:**
- Consumes: `JobStore` (Task 3), `ScheduleKind`, `JobOrigin` (Task 1).
- Produces: `ScheduleTaskTool` с `name = "schedule_task"`, `action_type = "schedule.create"`, `risk_level = RiskLevel.CRITICAL`; строка `"schedule.create"` в `CRITICAL_ACTIONS`.

- [ ] **Step 1: Написать падающий тест на политику**

В `tests/test_policy.py` добавить:

```python
def test_schedule_create_requires_approval_even_in_yolo(tmp_path: Path) -> None:
    """Блок D §7: джоба переживает run, поэтому её создание — critical-набор.

    Без этого гейта инъекция из файла получила бы способ закрепиться:
    запланировать работу, которая исполнится позже и с новым конфигом.
    """
    from svarog_harness.policy.engine import CRITICAL_ACTIONS

    assert "schedule.create" in CRITICAL_ACTIONS
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_policy.py -k schedule_create -v`
Expected: FAIL — `assert 'schedule.create' in frozenset({...})`

- [ ] **Step 3: Пополнить critical-набор**

В `src/svarog_harness/policy/engine.py` в `CRITICAL_ACTIONS` добавить элемент:

```python
        # Джоба переживает run и исполняется позже: без approval инъекция
        # получила бы способ закрепиться (ADR-0019, блок D §7).
        "schedule.create",
```

- [ ] **Step 4: Написать падающий тест на инструмент**

Создать `tests/test_schedule_tool.py`. Сценарии:

- вызов инструмента заводит джобу с `origin = JobOrigin.AGENT` и `enabled = False`;
- джоба наследует режим автономии создающего run'а — агент не может выдать себе больше прав;
- некорректное расписание возвращает ошибку инструмента, джоба не создаётся;
- `risk_level` инструмента — `CRITICAL`, `action_type` — `schedule.create`.

Реализующему: возьми за образец `tests/test_tools_base.py` и `tests/test_plan_tools.py` — там уже есть способ собрать инструмент и вызвать `call(...)`; используй те же приёмы.

- [ ] **Step 5: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_schedule_tool.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.tools.schedule_tools'`

- [ ] **Step 6: Написать инструмент**

Создать `src/svarog_harness/tools/schedule_tools.py`. Инструмент кладёт заявку в sink (как это делает `remember`), а не пишет в БД сам: применение — после завершения run'а, тем же способом, что и очередь памяти. Аргументы: `name`, `task`, вид расписания и его параметр, таймзона.

Джоба создаётся выключенной; в неё замораживаются режим автономии текущего run'а и текущий дайджест конфига.

- [ ] **Step 7: Зарегистрировать инструмент**

В `src/svarog_harness/runtime/orchestrator.py` зарегистрировать инструмент рядом с остальными (`registry.register(...)`), передав sink для заявок — тем же способом, каким туда передаются `memory_sink` и `plan_update_sink`.

- [ ] **Step 8: Запустить тесты**

Run: `uv run pytest tests/test_schedule_tool.py tests/test_policy.py tests/test_loop.py -v`
Expected: PASS

- [ ] **Step 9: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/tools/schedule_tools.py src/svarog_harness/policy/engine.py src/svarog_harness/runtime/orchestrator.py tests/test_schedule_tool.py tests/test_policy.py
git commit -m "feat(policy): gate agent-created jobs behind approval"
```

---

## Task 7: Системные джобы и защита скиллов

**Files:**
- Create: `src/svarog_harness/scheduler/system_jobs.py`
- Modify: `src/svarog_harness/skills/curator/pruning.py`
- Modify: `src/svarog_harness/cli/main.py` (вызов регистрации при старте демона)
- Test: `tests/test_scheduler_tick.py`, `tests/test_curator.py`

**Interfaces:**
- Consumes: `JobStore` (Task 3).
- Produces: `async ensure_system_jobs(store: JobStore, *, workspace: str, autonomy: str, config_digest: str, now: datetime) -> list[str]` — идемпотентная регистрация; функция в `pruning.py`, исключающая из архивации скиллы, использованные run'ами включённых джоб.

- [ ] **Step 1: Написать падающий тест на идемпотентность**

В `tests/test_scheduler_tick.py` добавить тест: двукратный вызов `ensure_system_jobs` не создаёт дубликатов; созданная джоба имеет `origin = JobOrigin.SYSTEM`, `protected = True`, `enabled = True`.

Системная джоба этого цикла одна — механический слой skill-куратора по интервалу (`TASK.md` §18.1).

Периодичности в `CuratorConfig` (`config/schema.py:196`) сегодня нет — там только пороги `stale_after_days` и `archive_after_days`. Добавь туда поле:

```python
    # Блок D: как часто системная джоба планировщика гоняет слой 1 (ADR-0019).
    prune_interval_sec: int = Field(default=86_400, gt=0)
```

Джоба заводится с `ScheduleKind.EVERY` и этим интервалом, задача — та же, что выполняет `svarog skills curate` без семантического слоя.

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_scheduler_tick.py -k system_jobs -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.scheduler.system_jobs'`

- [ ] **Step 3: Написать регистрацию**

Создать `src/svarog_harness/scheduler/system_jobs.py`: `ensure_system_jobs` ищет джобу по имени и `origin = SYSTEM`; если её нет — создаёт с `protected=True` и сразу включает (системная джоба не требует approval: её завёл код, а не агент). Повторный вызов ничего не меняет.

Вызов добавить в команду `svarog scheduler` перед входом в цикл.

- [ ] **Step 4: Написать падающий тест на защиту скиллов**

В `tests/test_curator.py` добавить тест: скилл, загружавшийся в run'е, который порождён включённой джобой, **не** переводится в `archived` даже при истёкшем сроке; тот же скилл без такой джобы — архивируется.

Реализующему: связь идёт `SkillLoad.run_id` → `Run.meta["cron_job_id"]` → `CronJob.enabled`. Возьми за образец существующие тесты `tests/test_curator.py` — там уже есть способ создать `SkillLoad` с нужной датой.

- [ ] **Step 5: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_curator.py -k cron -v`
Expected: FAIL — скилл архивируется, потому что защиты ещё нет

- [ ] **Step 6: Закрыть точку расширения в pruning**

В `src/svarog_harness/skills/curator/pruning.py` заменить комментарий-заглушку на реализацию: перед переводом скилла в `archived` проверить, не загружался ли он в run'ах включённых джоб. Комментарий о том, что «scheduled-задач в текущем срезе нет», убрать — он перестал быть правдой.

- [ ] **Step 7: Запустить тесты**

Run: `uv run pytest tests/test_curator.py tests/test_scheduler_tick.py -v`
Expected: PASS

- [ ] **Step 8: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/scheduler/system_jobs.py src/svarog_harness/skills/curator/pruning.py src/svarog_harness/cli/main.py tests/test_scheduler_tick.py tests/test_curator.py
git commit -m "feat(scheduler): register system jobs and protect their skills"
```

---

## Task 8: Документация

**Files:**
- Create: `docs/adr/0019-scheduler.md`
- Modify: `docs/adr/0010-yolo-first-autonomy.md`
- Modify: `TASK.md`
- Modify: `docs/reference-analysis.md`

**Interfaces:**
- Consumes: результаты задач 1-7.
- Produces: ничего исполняемого.

- [ ] **Step 1: Написать ADR-0019**

Создать `docs/adr/0019-scheduler.md` в структуре существующих ADR (Статус / Контекст / Решение / Последствия). Зафиксировать:

- планировщик — источник запуска run'ов, отличный от человека; отдельный демон `svarog scheduler`, `serve` джобы не исполняет и почему (единственный источник тика, никакой конкуренции двух процессов за одно расписание);
- замороженные права джобы (автономия + дайджест конфига), расхождение — fail-closed с отключением джобы; агентская джоба наследует автономию создающего run'а;
- `schedule.create` в неотключаемом critical-наборе: джоба переживает run, поэтому без гейта инъекция получила бы способ закрепиться;
- at-least-once без catch-up: догоняющего шторма после простоя демона не будет;
- занятый workspace — пропуск тика, а не ошибка; опирается на существующий per-workspace lease;
- два рубежа single-flight: межпроцессный файловый лок и compare-and-set по `next_run_at`;
- ограничение объёма: только интервал и «ежедневно в HH:MM», полный cron-синтаксис потребовал бы внешней зависимости; heartbeat-файл и внешние триггеры — вне цикла;
- файлы и тесты-репродьюсеры.

- [ ] **Step 2: Дополнить ADR-0010**

В `docs/adr/0010-yolo-first-autonomy.md`, в раздел «Неотключаемый critical-набор», добавить `schedule.create` в перечисление с обоснованием: джоба исполняется позже и с новым конфигом, поэтому её создание — необратимое по последствиям действие в смысле этого ADR.

- [ ] **Step 3: Обновить TASK.md**

- §18.1 — триггеры куратора стали реальными: механический слой запускается системной джобой планировщика; отметить, что триггер «после добавления N новых скиллов» по-прежнему не реализован, если это так;
- добавить секцию `scheduler` в пример конфигурации рядом с `supervisor`;
- строка 559 — исправить расхождение: Redis для очередей и локов не используется, фактическая реализация — SQLite и файловый advisory-lock (ADR-0007).

- [ ] **Step 4: Обновить reference-analysis**

В `docs/reference-analysis.md`, раздел про nanobot: перевести блок D из «отложено» в «перенесено». Указать: перенесено ядро расписания и системные джобы; заимствован приём defer-until-idle, но опирается на существующий lease; **не** перенесены heartbeat-файл и durable-очередь внешних триггеров; **не** перенесён свободный агентский доступ к созданию джоб — у Svarog он за неотключаемым approval, потому что модель безопасности замораживает права при старте run'а.

Проверить связность нумерации подразделов и что в списке «отложено» остался только блок C.

- [ ] **Step 5: Финальная проверка и коммит**

```bash
uv run ruff check && uv run pytest
git add docs/adr/0019-scheduler.md docs/adr/0010-yolo-first-autonomy.md TASK.md docs/reference-analysis.md
git commit -m "docs: add ADR-0019 for the scheduler"
```

---

## Self-Review (выполнено при написании плана)

**Покрытие спека:** §1 хранилище → Task 1; §2 расписание → Task 2; §3 демон и single-flight → Task 3 (compare-and-set) и Task 5 (лок, демон); §4 занятый workspace → Task 4; §5 без catch-up → Task 3 (`claim_due` считает от `now`); §6 замороженные права → Task 1 (поля), Task 4 (сверка); §7 агентский инструмент → Task 6; §8 системные джобы и защита скиллов → Task 7; §9 наблюдаемость и CLI → Task 5; §10 тестирование → распределено; §11 документация → Task 8. Пропусков нет.

**Согласованность типов:** `ScheduleKind`, `JobOrigin`, `CronJob` объявлены в Task 1 и используются в Tasks 2-7. `next_run_after(kind, spec, tz, now)` объявлена в Task 2 и вызывается в Task 3. `JobStore` с перечисленными методами объявлен в Task 3 и используется в Tasks 4, 5, 7. `JobRunRequest` и `tick(...)` объявлены в Task 4 и используются в Task 5.

**Известные места, требующие сверки с кодом при исполнении** (помечены как «Реализующему»): типы колонок `id`/`created_at` в миграции; способ объявления секции в `SvarogConfig`; фикстуры `tests/test_cli.py`; определение занятости workspace через lease; канал передачи метаданных run'а в `TaskRunner`; поле периодичности в `CuratorConfig`; приёмы сборки инструмента в тестах.

**Осознанное отступление от формата:** в задачах 5, 6 и 7 часть шагов описывает требования и точки сверки, а не готовый код — эти шаги правят большие существующие файлы (`cli/main.py` — свыше двух тысяч строк, `orchestrator.py`, `pruning.py`), где вставка вслепую по образцу опаснее, чем сверка с фактическим окружением. Все точные значения — имена, типы операций, флаги, статусы — заданы явно.
