"""Tool remember (§6.7): агент формирует MemoryChangeRequest, не пишет напрямую.

Заявка кладётся в sink; loop создаёт MemoryChange-строку в очереди SQLite,
единственный writer применяет и коммитит её после run (ADR-0004). Прямой
записи в memory-репозиторий у агента нет.
"""

from collections.abc import Callable

from pydantic import BaseModel, Field

from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.tools.base import RiskLevel, Tool, ToolResult

# loop подписывается, чтобы поставить заявку в очередь.
MemoryEnqueueCallback = Callable[[MemoryChangeRequest], None]


class RememberArgs(BaseModel):
    file: str = Field(description="Файл памяти относительно memory/, например user/profile.md")
    operation: MemoryOperation = Field(
        default=MemoryOperation.APPEND,
        description="create | append | replace_section | delete",
    )
    content: str = Field(default="", description="Содержимое для записи")
    section: str = Field(
        default="", description="Заголовок markdown-секции для replace_section (без #)"
    )


class RememberTool(Tool[RememberArgs]):
    name = "remember"
    action_type = "memory.write"
    description = (
        "Сохранить факт в долговременную память агента (memory-репозиторий); "
        "изменение применяется через контролируемую очередь"
    )
    risk_level = RiskLevel.LOW
    args_model = RememberArgs

    def __init__(self, on_enqueue: MemoryEnqueueCallback) -> None:
        self._on_enqueue = on_enqueue

    async def execute(self, args: RememberArgs) -> ToolResult:
        if args.operation is not MemoryOperation.DELETE and not args.content and not args.section:
            return ToolResult.failure("нужно указать content для записи в память")
        request = MemoryChangeRequest(
            file=args.file,
            operation=args.operation,
            content=args.content,
            section=args.section,
        )
        self._on_enqueue(request)
        return ToolResult.success(f"заявка в память принята: {request.summary()}")
