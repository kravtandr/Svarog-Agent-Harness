"""Инструмент планирования задач (блок D §7, ADR-0019).

Заявка кладётся в sink и материализуется джобой ПОСЛЕ завершения run'а — тем
же способом, что и очередь памяти (Flow A). Джоба создаётся выключенной:
`schedule.create` входит в неотключаемый critical-набор (ADR-0010), поэтому
без approval человека она не заработает.

Права джобы наследуются от текущего run'а: выдать себе больше, чем есть,
агент не может.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from svarog_harness.scheduler.schedule import ScheduleSpecError, parse_spec
from svarog_harness.storage.models import ScheduleKind
from svarog_harness.tools.base import RiskLevel, Tool, ToolResult

SCHEDULE_TOOL_NAME = "schedule_task"


@dataclass(frozen=True)
class ScheduleRequest:
    """Заявка на джобу, ожидающая approval и применения после run'а."""

    name: str
    task: str
    kind: ScheduleKind
    spec: str
    tz: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "task": self.task,
            "kind": self.kind.value,
            "spec": self.spec,
            "tz": self.tz,
        }


ScheduleEnqueueCallback = Callable[[ScheduleRequest], None]


class ScheduleTaskArgs(BaseModel):
    name: str = Field(description="Короткое имя джобы")
    task: str = Field(description="Задача, которую нужно выполнять по расписанию")
    every_seconds: int | None = Field(
        default=None, description="Интервал в секундах (взаимоисключающе с daily_at)"
    )
    daily_at: str | None = Field(
        default=None, description="Время суток HH:MM (взаимоисключающе с every_seconds)"
    )
    tz: str = Field(default="UTC", description="Таймзона расписания, например Europe/Moscow")


class ScheduleTaskTool(Tool[ScheduleTaskArgs]):
    name = SCHEDULE_TOOL_NAME
    action_type = "schedule.create"
    description = (
        "Запланировать регулярное выполнение задачи. Требует подтверждения "
        "человека в любом режиме автономии: джоба переживает текущую задачу и "
        "будет запускаться сама. Укажи ровно одно расписание — either "
        "every_seconds, either daily_at."
    )
    # Джоба переживает run — это необратимое по последствиям действие (ADR-0010).
    risk_level = RiskLevel.CRITICAL
    args_model = ScheduleTaskArgs

    def __init__(self, on_enqueue: ScheduleEnqueueCallback) -> None:
        self._on_enqueue = on_enqueue

    async def execute(self, args: ScheduleTaskArgs) -> ToolResult:
        if (args.every_seconds is None) == (args.daily_at is None):
            return ToolResult.failure("укажи ровно одно расписание: every_seconds ИЛИ daily_at")
        if args.every_seconds is not None:
            kind, spec = ScheduleKind.EVERY, str(args.every_seconds)
        else:
            kind, spec = ScheduleKind.DAILY_AT, str(args.daily_at)
        try:
            parse_spec(kind, spec)
        except ScheduleSpecError as exc:
            return ToolResult.failure(str(exc))

        self._on_enqueue(
            ScheduleRequest(name=args.name, task=args.task, kind=kind, spec=spec, tz=args.tz)
        )
        return ToolResult.success(
            f"заявка на джобу «{args.name}» принята; она создана выключенной и "
            f"заработает только после подтверждения человеком. Повторять заявку "
            f"не нужно."
        )
