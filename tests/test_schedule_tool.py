"""Агентский инструмент планирования (блок D §7)."""

from svarog_harness.storage.models import ScheduleKind
from svarog_harness.tools.base import RiskLevel
from svarog_harness.tools.schedule_tools import ScheduleRequest, ScheduleTaskTool


def _tool() -> tuple[ScheduleTaskTool, list[ScheduleRequest]]:
    sink: list[ScheduleRequest] = []
    return ScheduleTaskTool(on_enqueue=sink.append), sink


def test_tool_is_critical_and_typed() -> None:
    """Тип операции попадает в неотключаемый critical-набор политики."""
    tool, _ = _tool()
    assert tool.action_type == "schedule.create"
    assert tool.risk_level is RiskLevel.CRITICAL


async def test_enqueues_interval_request() -> None:
    tool, sink = _tool()
    result = await tool.call({"name": "сводка", "task": "собери", "every_seconds": 3600})

    assert result.ok
    assert len(sink) == 1
    assert sink[0].kind is ScheduleKind.EVERY
    assert sink[0].spec == "3600"


async def test_enqueues_daily_request() -> None:
    tool, sink = _tool()
    result = await tool.call(
        {"name": "ночная", "task": "собери", "daily_at": "03:00", "tz": "Europe/Moscow"}
    )

    assert result.ok
    assert sink[0].kind is ScheduleKind.DAILY_AT
    assert sink[0].spec == "03:00"
    assert sink[0].tz == "Europe/Moscow"


async def test_rejects_both_schedules() -> None:
    """Два расписания сразу — ошибка: молчаливый выбор одного из них опасен."""
    tool, sink = _tool()
    result = await tool.call({"name": "обе", "task": "t", "every_seconds": 60, "daily_at": "03:00"})

    assert not result.ok
    assert sink == []


async def test_rejects_no_schedule() -> None:
    tool, sink = _tool()
    result = await tool.call({"name": "никакого", "task": "t"})

    assert not result.ok
    assert sink == []


async def test_rejects_bad_daily_spec() -> None:
    tool, sink = _tool()
    result = await tool.call({"name": "плохая", "task": "t", "daily_at": "25:00"})

    assert not result.ok
    assert result.error is not None
    assert "HH:MM" in result.error
    assert sink == []


async def test_success_message_discourages_repeat() -> None:
    """Заявка отложенная: агент не должен повторять её «для надёжности»."""
    tool, _ = _tool()
    result = await tool.call({"name": "сводка", "task": "собери", "every_seconds": 3600})

    assert result.output
    assert "подтверждения" in result.output
    assert "Повторять" in result.output
