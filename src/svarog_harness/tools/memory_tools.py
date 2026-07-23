"""Tool remember (§6.7): агент формирует MemoryChangeRequest, не пишет напрямую.

Заявка кладётся в sink; loop создаёт MemoryChange-строку в очереди SQLite,
единственный writer применяет и коммитит её после run (ADR-0004). Прямой
записи в memory-репозиторий у агента нет. Поскольку применение происходит
после run (модель уже отчиталась пользователю), заявка валидируется по
текущему состоянию памяти прямо здесь — чтобы предсказуемые ошибки
(нет секции, create поверх существующего файла) вернулись модели сразу.
"""

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from svarog_harness.memory.apply import MemoryApplyError, resolve_memory_path
from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.proposal import MemoryProposalRequest, validate_proposal
from svarog_harness.memory.validate import validate_change
from svarog_harness.tools.base import RiskLevel, Tool, ToolResult, truncate_text

# Лимит вывода read_memory — как у файловых tools workspace.
_MAX_READ_CHARS = 50_000

# loop подписывается, чтобы поставить заявку в очередь.
MemoryEnqueueCallback = Callable[[MemoryChangeRequest], None]


class RememberArgs(BaseModel):
    file: str = Field(description="Файл памяти относительно memory/, например user/profile.md")
    operation: MemoryOperation = Field(
        default=MemoryOperation.APPEND,
        description="create | append | replace_section | delete",
    )
    content: str = Field(
        default="",
        description=(
            "Содержимое для записи. Для replace_section — ТОЛЬКО новое тело секции "
            "БЕЗ строки заголовка (заголовок сохраняется автоматически; повтор "
            "заголовка в content создаст дубль)"
        ),
    )
    section: str = Field(
        default="", description="Заголовок markdown-секции для replace_section (без #)"
    )
    field: str = Field(
        default="",
        description=(
            "Имя поля YAML-frontmatter для update_field (например status); "
            "новое значение поля передаётся в content"
        ),
    )


class RememberTool(Tool[RememberArgs]):
    name = "remember"
    action_type = "memory.write"
    description = (
        "Сохранить факт в долговременную память агента (memory-репозиторий); "
        "изменение применяется через контролируемую очередь ПОСЛЕ завершения run. "
        "Одну секцию правь одной заявкой: несколько replace_section на один и тот "
        "же section применятся последовательно поверх друг друга и испортят файл. "
        "Для replace_section в content кладётся только новое тело секции без "
        "строки её заголовка. Чтобы изменить одно поле frontmatter существующей "
        "страницы (например status) — используй update_field (field=имя, "
        "content=значение), НЕ delete+create: delete удалит страницу целиком, а "
        "create на существующий файл отклоняется. delete — только чтобы удалить "
        "сущность насовсем."
    )
    risk_level = RiskLevel.LOW
    args_model = RememberArgs

    def __init__(self, on_enqueue: MemoryEnqueueCallback, memory_dir: Path | None = None) -> None:
        self._on_enqueue = on_enqueue
        # Для валидации заявки по текущему состоянию памяти (чтение — без
        # ограничений, ADR-0004); None — валидация по файлам пропускается.
        self._memory_dir = memory_dir
        # Файлы, уже поставленные в очередь этим run'ом: очередь применяется
        # после run, поэтому цепочка create → replace_section по одному файлу
        # не должна ложно падать на проверке существования.
        self._pending_files: set[str] = set()

    async def execute(self, args: RememberArgs) -> ToolResult:
        if args.operation is MemoryOperation.UPDATE_FIELD and (not args.field or not args.content):
            return ToolResult.failure(
                "для update_field нужны field (имя поля) и content (новое значение)"
            )
        if args.operation is not MemoryOperation.DELETE and not args.content and not args.section:
            return ToolResult.failure("нужно указать content для записи в память")
        error = self._validate(args)
        if error is not None:
            return ToolResult.failure(error)
        request = MemoryChangeRequest(
            file=args.file,
            operation=args.operation,
            content=args.content,
            section=args.section,
            field=args.field,
        )
        self._on_enqueue(request)
        if self._memory_dir is not None and args.operation is not MemoryOperation.DELETE:
            self._pending_files.add(str(resolve_memory_path(self._memory_dir, args.file)))
        return ToolResult.success(
            f"заявка в память принята ({request.summary()}); применится после "
            f"завершения задачи. Не перечитывай файл через read_memory для проверки "
            f"и не повторяй заявку — считай изменение сделанным."
        )

    def _validate(self, args: RememberArgs) -> str | None:
        """Отловить предсказуемые ошибки применения до постановки в очередь."""
        if self._memory_dir is None:
            return None
        request = MemoryChangeRequest(
            file=args.file,
            operation=args.operation,
            content=args.content,
            section=args.section,
            field=args.field,
        )
        return validate_change(self._memory_dir, request, pending_files=self._pending_files)


class ReadMemoryArgs(BaseModel):
    path: str = Field(
        description="Путь к файлу памяти относительно memory/, "
        "например projects/<slug>/overview.md или decisions/<тема>.md"
    )


class ReadMemoryTool(Tool[ReadMemoryArgs]):
    """Прогрессивная загрузка памяти (ADR-0011): подтянуть страницу по требованию.

    Чтение памяти без ограничений (ADR-0004), поэтому tool low-risk и без
    approval. Список доступных страниц агент видит в index.md (горячий файл).
    """

    name = "read_memory"
    action_type = "memory.read"
    description = (
        "Прочитать файл долговременной памяти (страницу проекта, решение). "
        "Список всех страниц — в index.md, который уже в контексте."
    )
    risk_level = RiskLevel.LOW
    args_model = ReadMemoryArgs

    def is_read_only(self, args: ReadMemoryArgs) -> bool:
        return True

    def __init__(self, memory_dir: Path) -> None:
        self._memory_dir = memory_dir

    async def execute(self, args: ReadMemoryArgs) -> ToolResult:
        try:
            target = resolve_memory_path(self._memory_dir, args.path)
        except MemoryApplyError as exc:
            return ToolResult.failure(str(exc))
        if not target.is_file():
            return ToolResult.failure(f"файл памяти не найден: {args.path}")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult.failure(f"файл памяти не является текстовым (utf-8): {args.path}")
        except OSError as exc:
            return ToolResult.failure(f"не удалось прочитать '{args.path}': {exc}")
        return ToolResult.success(truncate_text(content, _MAX_READ_CHARS))


# Приёмник proposal'ов: orchestrator материализует их после run'а.
MemoryProposeCallback = Callable[[MemoryProposalRequest], None]


class MemoryChangeItem(BaseModel):
    """Одна правка внутри замысла. Поля совпадают с RememberArgs."""

    file: str = Field(description="Файл памяти относительно memory/")
    operation: MemoryOperation = Field(
        default=MemoryOperation.APPEND,
        description="create | append | replace_section | update_field | delete",
    )
    content: str = Field(default="", description="Содержимое; для update_field — значение поля")
    section: str = Field(default="", description="Заголовок секции для replace_section (без #)")
    field: str = Field(default="", description="Имя поля frontmatter для update_field")


class ProposeMemoryChangeArgs(BaseModel):
    title: str = Field(description="Краткое имя замысла — его человек видит в списке на ревью")
    rationale: str = Field(
        description="Зачем эта правка. Обязательно: человек читает proposal без контекста прогона"
    )
    changes: list[MemoryChangeItem] = Field(
        description="Правки одного связного замысла; несвязанные правки — отдельными вызовами"
    )


class ProposeMemoryChangeTool(Tool[ProposeMemoryChangeArgs]):
    """Предложить правку памяти на человеческое ревью (блок C, ADR-0020).

    Прямой записи у Dream нет: инструмент `remember` в его профиле не
    регистрируется. Один вызов = один замысел = один proposal.
    """

    name = "propose_memory_change"
    action_type = "memory.propose"
    description = (
        "Предложить изменение долговременной памяти на ревью человеку. "
        "Один вызов — один связный замысел: несколько правок допустимы, только "
        "если это части одного изменения (например, слияние двух страниц). "
        "Несвязанные правки оформляй отдельными вызовами — человек решает по "
        "каждому замыслу отдельно. Удаление непустой страницы запрещено: чтобы "
        "вывести проект из оборота, ставь status: archived через update_field."
    )
    risk_level = RiskLevel.LOW
    args_model = ProposeMemoryChangeArgs

    def __init__(self, on_propose: MemoryProposeCallback, memory_dir: Path) -> None:
        self._on_propose = on_propose
        self._memory_dir = memory_dir

    async def execute(self, args: ProposeMemoryChangeArgs) -> ToolResult:
        request = MemoryProposalRequest(
            title=args.title,
            rationale=args.rationale,
            changes=tuple(
                MemoryChangeRequest(
                    file=item.file,
                    operation=item.operation,
                    content=item.content,
                    section=item.section,
                    field=item.field,
                )
                for item in args.changes
            ),
        )
        # Валидация здесь, а не при материализации: ошибка должна вернуться
        # модели сразу, пока она может её исправить.
        errors = validate_proposal(self._memory_dir, request)
        if errors:
            return ToolResult.failure("; ".join(errors))
        self._on_propose(request)
        return ToolResult.success(
            f"предложение '{request.title}' принято ({len(request.changes)} правок); "
            f"оно ждёт решения человека. Не повторяй его и не проверяй результат "
            f"через read_memory — память изменится только после одобрения."
        )
