"""Run-local plan tool: краткий todo-list для сложных задач.

План — не память и не workspace-артефакт. Он живёт только внутри run'а,
попадает в checkpoint/resume и помогает модели не терять многошаговую задачу.
"""

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from svarog_harness.tools.base import RiskLevel, Tool, ToolResult

PlanStatus = Literal["pending", "in_progress", "completed", "blocked"]


class PlanItem(BaseModel):
    id: str = Field(description="Короткий стабильный id пункта, например 'inspect' или 'tests'")
    text: str = Field(description="Что нужно сделать; коротко и конкретно")
    status: PlanStatus = Field(description="pending | in_progress | completed | blocked")


class UpdatePlanArgs(BaseModel):
    items: list[PlanItem] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Полный текущий план. Передавай весь список целиком, а не только изменённый пункт"
        ),
    )
    note: str = Field(default="", description="Короткая причина изменения или текущий риск")

    @model_validator(mode="after")
    def validate_items(self) -> "UpdatePlanArgs":
        ids = [item.id for item in self.items]
        duplicates = sorted({item_id for item_id in ids if ids.count(item_id) > 1})
        if duplicates:
            raise ValueError(f"дублирующиеся id пунктов: {', '.join(duplicates)}")
        in_progress = [item for item in self.items if item.status == "in_progress"]
        if len(in_progress) > 1:
            raise ValueError("только один пункт может иметь status=in_progress")
        return self


PlanUpdateCallback = Callable[[list[dict[str, str]], str], None]


class UpdatePlanTool(Tool[UpdatePlanArgs]):
    name = "update_plan"
    action_type = "plan.update"
    description = (
        "Обновить run-local план для сложной многошаговой задачи. "
        "Используй только когда нужно отслеживать несколько этапов; не сохраняет файлы"
    )
    risk_level = RiskLevel.LOW
    args_model = UpdatePlanArgs

    def __init__(self, on_update: PlanUpdateCallback) -> None:
        self._on_update = on_update

    async def execute(self, args: UpdatePlanArgs) -> ToolResult:
        items = [item.model_dump() for item in args.items]
        self._on_update(items, args.note)
        if not items:
            return ToolResult.success("план очищен")

        lines = ["план обновлён:"]
        for item in items:
            lines.append(f"- [{item['status']}] {item['id']}: {item['text']}")
        if args.note:
            lines.append(f"note: {args.note}")
        return ToolResult.success("\n".join(lines))
