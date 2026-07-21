"""Расчёт следующего срабатывания (блок D §2). Время — параметр, не глобальное."""

from datetime import UTC, datetime

import pytest

from svarog_harness.scheduler.schedule import ScheduleSpecError, next_run_after, parse_spec
from svarog_harness.storage.models import ScheduleKind


def test_every_adds_interval() -> None:
    now = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)
    assert next_run_after(ScheduleKind.EVERY, "3600", "UTC", now) == datetime(
        2026, 7, 21, 11, 0, tzinfo=UTC
    )


def test_daily_at_picks_today_when_time_still_ahead() -> None:
    now = datetime(2026, 7, 21, 1, 0, tzinfo=UTC)
    assert next_run_after(ScheduleKind.DAILY_AT, "03:00", "UTC", now) == datetime(
        2026, 7, 21, 3, 0, tzinfo=UTC
    )


def test_daily_at_rolls_over_midnight_when_time_passed() -> None:
    """Время уже прошло сегодня — срабатывание переносится на завтра."""
    now = datetime(2026, 7, 21, 5, 0, tzinfo=UTC)
    assert next_run_after(ScheduleKind.DAILY_AT, "03:00", "UTC", now) == datetime(
        2026, 7, 22, 3, 0, tzinfo=UTC
    )


def test_daily_at_respects_timezone() -> None:
    """03:00 в Москве — это 00:00 UTC; на 01:00 UTC сегодняшнее уже прошло."""
    now = datetime(2026, 7, 21, 1, 0, tzinfo=UTC)
    result = next_run_after(ScheduleKind.DAILY_AT, "03:00", "Europe/Moscow", now)
    assert result == datetime(2026, 7, 22, 0, 0, tzinfo=UTC)


def test_daily_at_exactly_now_moves_to_next_day() -> None:
    """Граница: ровно время срабатывания — следующее уже завтра, без петли."""
    now = datetime(2026, 7, 21, 3, 0, tzinfo=UTC)
    assert next_run_after(ScheduleKind.DAILY_AT, "03:00", "UTC", now) == datetime(
        2026, 7, 22, 3, 0, tzinfo=UTC
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
    now = datetime(2026, 7, 21, 1, 0, tzinfo=UTC)
    with pytest.raises(ScheduleSpecError):
        next_run_after(ScheduleKind.DAILY_AT, "03:00", "Нигде/Такого", now)
