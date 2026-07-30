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


def request_from_args(args: ScheduleTaskArgs) -> ScheduleRequest:
    """Валидация аргументов → заявка; общий путь execute() и материализации
    одобренного approval на resume (orchestrator._claim_approved_schedule).

    Бросает ScheduleSpecError на «ноль или два расписания» и на кривой spec.
    """
    if (args.every_seconds is None) == (args.daily_at is None):
        raise ScheduleSpecError("укажи ровно одно расписание: every_seconds ИЛИ daily_at")
    if args.every_seconds is not None:
        kind, spec = ScheduleKind.EVERY, str(args.every_seconds)
    else:
        kind, spec = ScheduleKind.DAILY_AT, str(args.daily_at)
    parse_spec(kind, spec)
    return ScheduleRequest(name=args.name, task=args.task, kind=kind, spec=spec, tz=args.tz)


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
        try:
            request = request_from_args(args)
        except ScheduleSpecError as exc:
            return ToolResult.failure(str(exc))

        self._on_enqueue(request)
        # К моменту исполнения approval уже получен: schedule.create — critical-
        # набор, гейт стоит ДО execute и в native loop, и на MCP-мосте. Прежний
        # текст («создана выключенной, заработает после подтверждения») врал и
        # провоцировал повторные заявки после resume (каскад S18/S24, 30.07.2026).
        return ToolResult.success(
            f"джоба «{args.name}» одобрена человеком; сразу после завершения "
            f"этого run'а она будет создана включённой и начнёт запускаться по "
            f"расписанию. Планирование ВЫПОЛНЕНО: не вызывай schedule_task "
            f"повторно для этой задачи, даже с изменённой формулировкой — "
            f"повтор создаст дубль джобы."
        )
