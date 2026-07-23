# Memory-proposals и Dream — план реализации (блок C)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** дать памяти семантический слой — фоновый агент Dream, который предлагает правки памяти, а применяются они только после явного решения человека.

**Architecture:** proposal — это отложенная пачка `MemoryChangeRequest` в SQLite, а не ветка git: одобрение перекладывает заявки в штатную очередь единственного писателя (ADR-0004), отклонение просто меняет статус. Dream — обычный run с урезанным реестром инструментов (`RunProfile.DREAM`: только `read_memory` и `propose_memory_change`), запускаемый защищённой системной джобой планировщика.

**Tech Stack:** Python 3.12+, SQLAlchemy async + alembic, pydantic v2 (`StrictModel`), typer + rich (CLI), pytest + pytest-asyncio, uv.

## Global Constraints

- Спека: `docs/superpowers/specs/2026-07-23-memory-proposals-and-dream-design.md`. При расхождении плана со спекой — прав спек, останавливаемся и спрашиваем.
- Комментарии, докстринги и сообщения пользователю — **на русском**. Комментарий объясняет *почему*, а не *что* (что видно из кода).
- `line-length = 100` (ruff), `mypy strict = true`. Обе проверки обязаны быть зелёными перед каждым коммитом.
- Проверки запускать так, чтобы код возврата был от инструмента, а не от `tail`:
  `set -e; uv run ruff check; uv run ruff format --check; uv run mypy; uv run pytest -q`
- Никаких секретов в коде, тестах и фикстурах.
- Тесты, трогающие память, работают **только** в `tmp_path`. Никогда против реального `agent-home/memory`.
- Ветка работы: `feat/memory-dream` (уже создана, в ней лежит спек-коммит `a12d019`).
- Новых зависимостей не добавляем.

## Структура файлов

**Создаются:**

| файл | ответственность |
|---|---|
| `src/svarog_harness/memory/validate.py` | единая валидация одной заявки по текущему состоянию памяти |
| `src/svarog_harness/memory/proposal.py` | `MemoryProposalRequest` + правила допустимости proposal'а |
| `src/svarog_harness/memory/proposal_manager.py` | персист, выборка, решение человека, постановка в очередь |
| `src/svarog_harness/memory/dream.py` | текст задачи Dream из находок аудита |
| `src/svarog_harness/storage/migrations/versions/a7c9e1d5b2f8_add_memory_proposals.py` | таблица `memory_proposals` |
| `tests/test_memory_validate.py` | тесты общей валидации |
| `tests/test_memory_proposal.py` | тесты правил §3 |
| `tests/test_memory_proposal_manager.py` | тесты persist/approve/reject/устаревание |
| `tests/test_dream_profile.py` | тесты профиля реестра и текста задачи |
| `tests/test_cli_memory_proposals.py` | тесты CLI-команд |

**Модифицируются:**

| файл | что |
|---|---|
| `src/svarog_harness/storage/models.py` | `MemoryProposalStatus`, `MemoryProposalOrigin`, `MemoryProposal` |
| `src/svarog_harness/gitflow/repo.py` | `head_sha()` |
| `src/svarog_harness/tools/memory_tools.py` | `RememberTool._validate` → общая функция; `ProposeMemoryChangeTool` |
| `src/svarog_harness/memory/__init__.py` | реэкспорт новых сущностей |
| `src/svarog_harness/runtime/orchestrator.py` | `RunProfile`, ветвление реестра, `drain_memory_proposals` |
| `src/svarog_harness/scheduler/system_jobs.py` | `DREAM_JOB_NAME` и заводка под гейтом |
| `src/svarog_harness/config/schema.py` | `DreamConfig` |
| `src/svarog_harness/cli/main.py` | `memory proposals *`, диспетчеризация профиля, уведомление |

**Порядок задач** — снизу вверх: данные → валидация → менеджер → инструмент → профиль → джоба → CLI → документы. Каждая задача заканчивается зелёными проверками и коммитом.

---

### Task 1: Таблица `memory_proposals`

**Files:**
- Modify: `src/svarog_harness/storage/models.py` (добавить после класса `SkillProposal`, ~строка 361)
- Create: `src/svarog_harness/storage/migrations/versions/a7c9e1d5b2f8_add_memory_proposals.py`
- Test: `tests/test_memory_proposal_manager.py`

**Interfaces:**
- Consumes: `TimestampedBase`, `_enum`, `utcnow` из `storage/models.py`.
- Produces: `MemoryProposal` (ORM), `MemoryProposalStatus` (`pending`/`applied`/`rejected`/`failed`), `MemoryProposalOrigin` (`dream`).

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_memory_proposal_manager.py`:

```python
"""Тесты memory-proposals (блок C, ADR-0020): отложенные правки памяти под ревью."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import (
    MemoryProposal,
    MemoryProposalOrigin,
    MemoryProposalStatus,
)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_memory_proposal_row_roundtrip(db: AsyncSession) -> None:
    """Строка proposal'а сохраняется и читается со всеми полями решения."""
    row = MemoryProposal(
        title="слить дубли проекта",
        rationale="две страницы описывают один бот",
        changes=[{"file": "projects/a/overview.md", "operation": "append", "content": "x"}],
        origin=MemoryProposalOrigin.DREAM,
        memory_head="deadbeef",
        checks={"validation": []},
    )
    db.add(row)
    await db.commit()

    found = (await db.execute(select(MemoryProposal))).scalar_one()
    assert found.status is MemoryProposalStatus.PENDING
    assert found.origin is MemoryProposalOrigin.DREAM
    assert found.title == "слить дубли проекта"
    assert found.changes[0]["file"] == "projects/a/overview.md"
    assert found.memory_head == "deadbeef"
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_memory_proposal_manager.py -q`
Expected: FAIL — `ImportError: cannot import name 'MemoryProposal'`

- [ ] **Step 3: Добавить enum'ы и модель**

В `src/svarog_harness/storage/models.py` рядом с `SkillProposalStatus` (~строка 84) добавить:

```python
class MemoryProposalStatus(StrEnum):
    """Статус memory proposal (блок C, ADR-0020).

    Отличается от SkillProposalStatus по существу: у скиллов одобрение — merge
    ветки, у памяти — постановка заявок в очередь единственного писателя.
    """

    PENDING = "pending"  # ждёт решения человека
    APPLIED = "applied"  # заявки поставлены в очередь writer'а
    REJECTED = "rejected"  # отклонён, память не тронута
    FAILED = "failed"  # не прошёл валидацию


class MemoryProposalOrigin(StrEnum):
    """Кто предложил правку. В первом срезе на ревью уходит только Dream."""

    DREAM = "dream"
```

После класса `SkillProposal` (~строка 361) добавить:

```python
class MemoryProposal(TimestampedBase):
    """Отложенная пачка заявок в память, ждущая решения человека (ADR-0020).

    В отличие от skill proposal, содержимое не материализуется в git-ветке:
    ветвление в memory-репозитории столкнулось бы с writer'ом, который
    непрерывно коммитит в текущую ветку (ADR-0004). Заявки лежат здесь, и
    одобрение перекладывает их в штатную очередь `memory_queue`.
    """

    __tablename__ = "memory_proposals"

    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    rationale: Mapped[str] = mapped_column(Text)
    # Список MemoryChangeRequest.to_dict() — один связный замысел.
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[MemoryProposalStatus] = mapped_column(
        _enum(MemoryProposalStatus), default=MemoryProposalStatus.PENDING, index=True
    )
    origin: Mapped[MemoryProposalOrigin] = mapped_column(
        _enum(MemoryProposalOrigin), default=MemoryProposalOrigin.DREAM, index=True
    )
    # HEAD памяти на момент предложения: расхождение с текущим — сигнал, что
    # состояние ушло вперёд и предпросмотр надо смотреть внимательнее.
    memory_head: Mapped[str | None] = mapped_column(String(64))
    checks: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # id строк memory_queue, порождённых одобрением — след для аудита.
    applied_change_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    decided_at: Mapped[datetime | None]
    decided_by: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)
```

Если `JSON` ещё не импортирован в модуле — он уже есть в `type_annotation_map`; для явных `mapped_column(JSON, ...)` добавить `JSON` в импорт из `sqlalchemy`.

- [ ] **Step 4: Написать миграцию**

Создать `src/svarog_harness/storage/migrations/versions/a7c9e1d5b2f8_add_memory_proposals.py`:

```python
"""add memory_proposals

Revision ID: a7c9e1d5b2f8
Revises: f6b8d3e2a9c4
Create Date: 2026-07-23 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c9e1d5b2f8"
down_revision: str | None = "f6b8d3e2a9c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_proposals",
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "applied",
                "rejected",
                "failed",
                name="memoryproposalstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "origin",
            sa.Enum("dream", name="memoryproposalorigin", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("memory_head", sa.String(length=64), nullable=True),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("applied_change_ids", sa.JSON(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_proposals_run_id", "memory_proposals", ["run_id"], unique=False)
    op.create_index("ix_memory_proposals_status", "memory_proposals", ["status"], unique=False)
    op.create_index("ix_memory_proposals_origin", "memory_proposals", ["origin"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_memory_proposals_origin", table_name="memory_proposals")
    op.drop_index("ix_memory_proposals_status", table_name="memory_proposals")
    op.drop_index("ix_memory_proposals_run_id", table_name="memory_proposals")
    op.drop_table("memory_proposals")
```

- [ ] **Step 5: Запустить тест — должен пройти**

Run: `uv run pytest tests/test_memory_proposal_manager.py -q`
Expected: PASS (1 passed)

- [ ] **Step 6: Проверить, что миграция и модель не разъехались**

Run: `uv run pytest -q -k "migration or storage or db"`
Expected: PASS. Если в проекте есть тест, сверяющий `Base.metadata` с миграциями, он поймает расхождение — при провале сверить имена колонок буквально.

- [ ] **Step 7: Полные проверки и коммит**

```bash
set -e
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -q
git add src/svarog_harness/storage/models.py \
        src/svarog_harness/storage/migrations/versions/a7c9e1d5b2f8_add_memory_proposals.py \
        tests/test_memory_proposal_manager.py
git commit -m "feat(memory): таблица memory_proposals для отложенных правок памяти"
```

---

### Task 2: Общая валидация заявки — `validate_change`

Правила контракта страницы проекта не должны разъехаться между двумя путями записи (`remember` и `propose_memory_change`). Поэтому валидация выносится из метода инструмента в функцию.

**Files:**
- Create: `src/svarog_harness/memory/validate.py`
- Modify: `src/svarog_harness/tools/memory_tools.py:113-180` (`RememberTool._validate` → делегирование)
- Test: `tests/test_memory_validate.py`

**Interfaces:**
- Consumes: `MemoryChangeRequest`, `MemoryOperation`, `resolve_memory_path`, `has_section`, `preview_content`, `MemoryApplyError`, `project_slug_from_path`, `validate_project_page`.
- Produces: `validate_change(memory_dir: Path, request: MemoryChangeRequest, *, pending_files: set[str] | None = None) -> str | None` — `None` значит «валидна», строка — сообщение об ошибке для модели.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_memory_validate.py`:

```python
"""Тесты общей валидации заявки памяти (блок C): один свод правил на оба пути записи."""

from pathlib import Path

from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.validate import validate_change


def _req(file: str, op: MemoryOperation, **kw: str) -> MemoryChangeRequest:
    return MemoryChangeRequest(file=file, operation=op, **kw)


def test_create_over_existing_file_rejected(tmp_path: Path) -> None:
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "profile.md").write_text("есть\n", encoding="utf-8")
    error = validate_change(
        tmp_path, _req("user/profile.md", MemoryOperation.CREATE, content="новое")
    )
    assert error is not None and "уже существует" in error


def test_replace_section_without_section_rejected(tmp_path: Path) -> None:
    error = validate_change(
        tmp_path, _req("user/profile.md", MemoryOperation.REPLACE_SECTION, content="тело")
    )
    assert error is not None and "section" in error


def test_sources_are_immutable(tmp_path: Path) -> None:
    """sources/ — raw-слой ADR-0011: правки запрещены, только create нового файла."""
    error = validate_change(
        tmp_path, _req("sources/spec/a.md", MemoryOperation.APPEND, content="хвост")
    )
    assert error is not None and "неизменяемый" in error


def test_pending_file_relaxes_existence_check(tmp_path: Path) -> None:
    """Файл, поставленный в очередь этим же run'ом, ещё не на диске — не ошибка."""
    target = str((tmp_path / "notes.md").resolve())
    error = validate_change(
        tmp_path,
        _req("notes.md", MemoryOperation.UPDATE_FIELD, field="status", content="active"),
        pending_files={target},
    )
    assert error is None


def test_valid_append_passes(tmp_path: Path) -> None:
    assert validate_change(tmp_path, _req("notes.md", MemoryOperation.APPEND, content="x")) is None
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_memory_validate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.memory.validate'`

- [ ] **Step 3: Создать модуль, перенеся тело `RememberTool._validate`**

Создать `src/svarog_harness/memory/validate.py`. Тело — дословный перенос `RememberTool._validate` (`tools/memory_tools.py:113-180`) с заменой `self._memory_dir` на параметр и `self._pending_files` на `pending_files`:

```python
"""Единая валидация заявки памяти по текущему состоянию (§6.7, ADR-0011).

Оба пути записи — прямой `remember` и `propose_memory_change` под ревью —
обязаны применять один свод правил. Иначе контракт страницы проекта со
временем разъедется между ними.
"""

from pathlib import Path

from svarog_harness.memory.apply import (
    MemoryApplyError,
    has_section,
    preview_content,
    resolve_memory_path,
)
from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.project_page import project_slug_from_path, validate_project_page


def validate_change(
    memory_dir: Path,
    request: MemoryChangeRequest,
    *,
    pending_files: set[str] | None = None,
) -> str | None:
    """Отловить предсказуемые ошибки применения до постановки в очередь.

    `pending_files` — абсолютные пути, уже поставленные в очередь этим же
    run'ом: очередь применяется после run, поэтому цепочка create →
    replace_section по одному файлу не должна ложно падать на проверке
    существования. None — проверять строго по диску.
    """
    queued = pending_files or set()
    try:
        target = resolve_memory_path(memory_dir, request.file)
    except MemoryApplyError as exc:
        return str(exc)

    if request.file.split("/", 1)[0] == "sources" and request.operation in (
        MemoryOperation.APPEND,
        MemoryOperation.REPLACE_SECTION,
        MemoryOperation.UPDATE_FIELD,
    ):
        # sources/ — raw-слой (ADR-0011): исходники неизменяемы, правки
        # запрещены. Нужен новый вариант — create нового файла.
        return (
            f"'{request.file}' в sources/ — неизменяемый исходник; "
            f"правки запрещены, создай новый файл через create"
        )

    if request.operation is MemoryOperation.CREATE and target.exists():
        return (
            f"файл '{request.file}' уже существует; create перезаписывает файл "
            f"целиком — используй append или replace_section"
        )

    if request.operation is MemoryOperation.REPLACE_SECTION:
        if not request.section:
            return "для replace_section нужно указать section"
        if target.exists():
            text = target.read_text(encoding="utf-8")
            if not has_section(text, request.section):
                return (
                    f"секция '{request.section}' не найдена в '{request.file}'; "
                    f"проверь заголовок или используй append"
                )
        elif str(target) not in queued:
            return f"файл '{request.file}' не существует для replace_section"

    if request.operation is MemoryOperation.UPDATE_FIELD:
        if not request.field:
            return "для update_field нужно указать field (имя поля frontmatter)"
        if not target.exists() and str(target) not in queued:
            return f"файл '{request.file}' не существует для update_field"

    slug = project_slug_from_path(request.file)
    if slug is not None and request.operation is not MemoryOperation.DELETE:
        # Контракт страницы проекта (ADR-0011): frontmatter должен быть валиден
        # в прогнозируемом содержимом. Заявку, поставленную в очередь этим же
        # run'ом и ещё не применённую, пропускаем — её провалидирует своя заявка.
        if str(target) in queued and not target.exists():
            return None
        try:
            prospective = preview_content(memory_dir, request)
        except MemoryApplyError as exc:
            return str(exc)
        return validate_project_page(prospective, expected_slug=slug)
    return None
```

- [ ] **Step 4: Переключить `RememberTool` на общую функцию**

В `src/svarog_harness/tools/memory_tools.py` заменить весь метод `_validate` (строки 113-180) на:

```python
    def _validate(self, args: RememberArgs) -> str | None:
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
```

Импорты в шапке модуля: убрать ставшие ненужными (`has_section`, `preview_content`, `project_slug_from_path`, `validate_project_page`), добавить `from svarog_harness.memory.validate import validate_change`. `resolve_memory_path` и `MemoryApplyError` остаются — они используются в `execute` и в `ReadMemoryTool`.

- [ ] **Step 5: Запустить новые и старые тесты**

Run: `uv run pytest tests/test_memory_validate.py tests/test_memory.py tests/test_memory_wiki.py -q`
Expected: PASS. Старые тесты `remember` обязаны остаться зелёными без правок — если какой-то упал, значит перенос неточен, сверить сообщение об ошибке буквально.

- [ ] **Step 6: Полные проверки и коммит**

```bash
set -e
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -q
git add src/svarog_harness/memory/validate.py \
        src/svarog_harness/tools/memory_tools.py \
        tests/test_memory_validate.py
git commit -m "refactor(memory): единая validate_change для обоих путей записи"
```

---

### Task 3: `MemoryProposalRequest` и правила допустимости

**Files:**
- Create: `src/svarog_harness/memory/proposal.py`
- Test: `tests/test_memory_proposal.py`

**Interfaces:**
- Consumes: `validate_change` (Task 2), `MemoryChangeRequest`, `MemoryOperation`, `resolve_memory_path`.
- Produces:
  - `MemoryProposalRequest(title: str, rationale: str, changes: tuple[MemoryChangeRequest, ...], source_run_id: str | None = None)` с методом `to_changes_json() -> list[dict[str, Any]]`;
  - `validate_proposal(memory_dir: Path, request: MemoryProposalRequest) -> list[str]` — пустой список означает «валиден».

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_memory_proposal.py`:

```python
"""Тесты правил допустимости memory proposal (блок C §3)."""

from pathlib import Path

from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.proposal import MemoryProposalRequest, validate_proposal


def _proposal(*changes: MemoryChangeRequest, title: str = "правка", rationale: str = "зачем") -> (
    MemoryProposalRequest
):
    return MemoryProposalRequest(title=title, rationale=rationale, changes=tuple(changes))


def _append(file: str = "notes.md") -> MemoryChangeRequest:
    return MemoryChangeRequest(file=file, operation=MemoryOperation.APPEND, content="текст")


def test_valid_proposal_has_no_errors(tmp_path: Path) -> None:
    assert validate_proposal(tmp_path, _proposal(_append())) == []


def test_empty_rationale_rejected(tmp_path: Path) -> None:
    """Человек, читающий proposal через месяц, должен видеть зачем правка."""
    errors = validate_proposal(tmp_path, _proposal(_append(), rationale="   "))
    assert any("rationale" in e for e in errors)


def test_empty_changes_rejected(tmp_path: Path) -> None:
    errors = validate_proposal(tmp_path, _proposal())
    assert any("ни одной правки" in e for e in errors)


def test_delete_of_non_empty_file_rejected(tmp_path: Path) -> None:
    """Удаление содержательной страницы — потеря, а не консолидация (ADR-0009)."""
    (tmp_path / "projects" / "a").mkdir(parents=True)
    page = tmp_path / "projects" / "a" / "overview.md"
    page.write_text("---\nname: a\n---\nсодержимое\n", encoding="utf-8")
    errors = validate_proposal(
        tmp_path,
        _proposal(
            MemoryChangeRequest(
                file="projects/a/overview.md", operation=MemoryOperation.DELETE
            )
        ),
    )
    assert any("archived" in e for e in errors)


def test_delete_of_empty_file_allowed(tmp_path: Path) -> None:
    """Находка `empty` структурного аудита — единственный законный случай delete."""
    (tmp_path / "junk.md").write_text("   \n", encoding="utf-8")
    errors = validate_proposal(
        tmp_path,
        _proposal(MemoryChangeRequest(file="junk.md", operation=MemoryOperation.DELETE)),
    )
    assert errors == []


def test_change_level_error_is_reported_with_index(tmp_path: Path) -> None:
    """Ошибка отдельной правки называет её номер — иначе непонятно, какая из пачки."""
    (tmp_path / "notes.md").write_text("есть\n", encoding="utf-8")
    errors = validate_proposal(
        tmp_path,
        _proposal(
            _append(),
            MemoryChangeRequest(
                file="notes.md", operation=MemoryOperation.CREATE, content="перезапись"
            ),
        ),
    )
    assert any(e.startswith("правка 2:") for e in errors)
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_memory_proposal.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.memory.proposal'`

- [ ] **Step 3: Реализовать**

Создать `src/svarog_harness/memory/proposal.py`:

```python
"""Memory proposal (блок C, ADR-0020): отложенная пачка правок под ревью.

Dream не пишет в память — он предлагает связный замысел, который применяется
только после решения человека. Один вызов инструмента = один proposal = один
замысел, возможно из нескольких правок.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from svarog_harness.memory.apply import MemoryApplyError, resolve_memory_path
from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.validate import validate_change


@dataclass(frozen=True)
class MemoryProposalRequest:
    title: str
    rationale: str
    changes: tuple[MemoryChangeRequest, ...] = field(default_factory=tuple)
    source_run_id: str | None = None

    def to_changes_json(self) -> list[dict[str, Any]]:
        return [change.to_dict() for change in self.changes]


def _delete_allowed(memory_dir: Path, request: MemoryChangeRequest) -> str | None:
    """delete проходит только для пустого (или отсутствующего) файла.

    Перенос инварианта ADR-0009 «никогда не удаляет — только архивирует» в
    память: для «проект закончился» есть update_field status=archived, страница
    при этом остаётся и уходит в раздел архива index.md (ADR-0011). Правило
    живёт в коде, а не в промте: модель не должна иметь возможности обойти его
    собственной интерпретацией.
    """
    try:
        target = resolve_memory_path(memory_dir, request.file)
    except MemoryApplyError as exc:
        return str(exc)
    if not target.exists():
        return None
    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Нечитаемый файл содержательным не считаем — удалить его законно.
        return None
    if content.strip():
        return (
            f"удаление непустой страницы '{request.file}' запрещено; "
            f"чтобы вывести её из оборота, поставь status: archived через update_field"
        )
    return None


def validate_proposal(memory_dir: Path, request: MemoryProposalRequest) -> list[str]:
    """Проверить proposal целиком; пустой список — валиден."""
    errors: list[str] = []
    if not request.title.strip():
        errors.append("title обязателен: он единственное, что видно в списке на ревью")
    if not request.rationale.strip():
        errors.append(
            "rationale обязателен: человек должен видеть, зачем правка, а не только что она делает"
        )
    if not request.changes:
        errors.append("proposal не содержит ни одной правки")
        return errors

    for index, change in enumerate(request.changes, start=1):
        if change.operation is MemoryOperation.DELETE:
            error = _delete_allowed(memory_dir, change)
        else:
            error = validate_change(memory_dir, change)
        if error is not None:
            errors.append(f"правка {index}: {error}")
    return errors
```

- [ ] **Step 4: Запустить тест — должен пройти**

Run: `uv run pytest tests/test_memory_proposal.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Полные проверки и коммит**

```bash
set -e
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -q
git add src/svarog_harness/memory/proposal.py tests/test_memory_proposal.py
git commit -m "feat(memory): MemoryProposalRequest и правила допустимости (delete только для пустых)"
```

---

### Task 4: `head_sha` и `MemoryProposalManager`

**Files:**
- Modify: `src/svarog_harness/gitflow/repo.py` (после `has_commits`, ~строка 122)
- Create: `src/svarog_harness/memory/proposal_manager.py`
- Modify: `src/svarog_harness/memory/__init__.py`
- Test: `tests/test_memory_proposal_manager.py` (дополнить тестами из Task 1)

**Interfaces:**
- Consumes: `MemoryProposalRequest`, `validate_proposal` (Task 3); `MemoryProposal`, `MemoryProposalStatus` (Task 1); `MemoryWriter.enqueue`; `preview_content`.
- Produces:
  - `GitRepo.head_sha() -> str | None`;
  - `MemoryProposalManager(db, memory_dir)` с методами `persist(request) -> MemoryProposal`, `list_pending(limit=50) -> list[MemoryProposal]`, `pending_count() -> int`, `get(prefix) -> MemoryProposal`, `decide(proposal, *, approved, decided_by, reason=None) -> list[str]`, `preview(proposal) -> list[tuple[str, str]]`, `head_moved(proposal) -> bool`;
  - исключения `MemoryProposalNotFoundError`, `MemoryProposalStateError`;
  - статический `validation_messages(proposal) -> list[str]`.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `tests/test_memory_proposal_manager.py`:

```python
from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.proposal import MemoryProposalRequest
from svarog_harness.memory.proposal_manager import (
    MemoryProposalManager,
    MemoryProposalStateError,
)
from svarog_harness.memory.writer import MemoryWriter
from svarog_harness.storage.models import MemoryChange, MemoryChangeStatus


def _request(*changes: MemoryChangeRequest) -> MemoryProposalRequest:
    return MemoryProposalRequest(
        title="правка", rationale="потому что", changes=tuple(changes)
    )


def _append(file: str = "notes.md", content: str = "новый факт") -> MemoryChangeRequest:
    return MemoryChangeRequest(file=file, operation=MemoryOperation.APPEND, content=content)


async def test_persist_valid_proposal_is_pending(db: AsyncSession, tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    row = await MemoryProposalManager(db, mem).persist(_request(_append()))
    assert row.status is MemoryProposalStatus.PENDING
    assert row.changes[0]["operation"] == "append"


async def test_persist_invalid_proposal_is_failed(db: AsyncSession, tmp_path: Path) -> None:
    """Невалидный proposal не теряется: он записан со статусом failed и причинами."""
    mem = tmp_path / "memory"
    mem.mkdir()
    row = await MemoryProposalManager(db, mem).persist(
        MemoryProposalRequest(title="", rationale="", changes=())
    )
    assert row.status is MemoryProposalStatus.FAILED
    assert MemoryProposalManager.validation_messages(row)


async def test_approve_enqueues_changes(db: AsyncSession, tmp_path: Path) -> None:
    """Одобрение перекладывает заявки в штатную очередь единственного писателя."""
    mem = tmp_path / "memory"
    mem.mkdir()
    manager = MemoryProposalManager(db, mem)
    row = await manager.persist(_request(_append(), _append("other.md")))

    ids = await manager.decide(row, approved=True, decided_by="cli")

    assert len(ids) == 2
    assert row.status is MemoryProposalStatus.APPLIED
    assert row.applied_change_ids == ids
    queued = (await db.execute(select(MemoryChange))).scalars().all()
    assert [q.status for q in queued] == [MemoryChangeStatus.PENDING] * 2


async def test_reject_touches_nothing(db: AsyncSession, tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    manager = MemoryProposalManager(db, mem)
    row = await manager.persist(_request(_append()))

    ids = await manager.decide(row, approved=False, decided_by="cli", reason="не надо")

    assert ids == []
    assert row.status is MemoryProposalStatus.REJECTED
    assert row.reason == "не надо"
    assert (await db.execute(select(MemoryChange))).scalars().all() == []
    assert not (mem / "notes.md").exists()


async def test_second_decision_rejected(db: AsyncSession, tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    manager = MemoryProposalManager(db, mem)
    row = await manager.persist(_request(_append()))
    await manager.decide(row, approved=False, decided_by="cli")

    with pytest.raises(MemoryProposalStateError):
        await manager.decide(row, approved=True, decided_by="cli")


async def test_preview_is_computed_on_current_state(db: AsyncSession, tmp_path: Path) -> None:
    """Предпросмотр считается на текущей памяти, а не замораживается при создании."""
    mem = tmp_path / "memory"
    mem.mkdir()
    manager = MemoryProposalManager(db, mem)
    row = await manager.persist(_request(_append(content="хвост")))

    # Память ушла вперёд уже после предложения.
    (mem / "notes.md").write_text("голова\n", encoding="utf-8")

    previews = manager.preview(row)
    assert len(previews) == 1
    path, text = previews[0]
    assert path == "notes.md"
    assert "голова" in text and "хвост" in text


async def test_preview_reports_inapplicable_change(db: AsyncSession, tmp_path: Path) -> None:
    """Неприменимая правка показывается человеку как таковая, а не роняет show."""
    mem = tmp_path / "memory"
    mem.mkdir()
    manager = MemoryProposalManager(db, mem)
    row = await manager.persist(
        _request(
            MemoryChangeRequest(
                file="notes.md", operation=MemoryOperation.APPEND, content="x"
            )
        )
    )
    row.changes = [
        {
            "file": "gone.md",
            "operation": "replace_section",
            "content": "тело",
            "section": "Раздел",
            "field": "",
        }
    ]

    previews = manager.preview(row)
    assert "не применима" in previews[0][1]


async def test_applied_changes_reach_files_after_drain(db: AsyncSession, tmp_path: Path) -> None:
    """Сквозной путь: approve → очередь → drain → файл на диске."""
    mem = tmp_path / "memory"
    mem.mkdir()
    manager = MemoryProposalManager(db, mem)
    row = await manager.persist(_request(_append(content="строка")))
    await manager.decide(row, approved=True, decided_by="cli")

    await MemoryWriter(db, mem).drain()

    assert "строка" in (mem / "notes.md").read_text(encoding="utf-8")
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_memory_proposal_manager.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.memory.proposal_manager'`

- [ ] **Step 3: Добавить `head_sha` в `GitRepo`**

В `src/svarog_harness/gitflow/repo.py` сразу после `has_commits` добавить:

```python
    async def head_sha(self) -> str | None:
        """SHA текущего HEAD (None — репозитория нет или в нём нет коммитов)."""
        code, out, _ = await self._git("rev-parse", "HEAD", check=False)
        return out.strip() if code == 0 and out.strip() else None
```

- [ ] **Step 4: Реализовать менеджер**

Создать `src/svarog_harness/memory/proposal_manager.py`:

```python
"""Governance memory proposals (блок C, ADR-0020): персист, ревью, применение.

Одобрение не пишет в память напрямую: оно перекладывает заявки в очередь
единственного writer'а (ADR-0004), поэтому применение, secret scan, коммит и
перегенерация index.md идут штатным путём.
"""

from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.memory.apply import MemoryApplyError, preview_content
from svarog_harness.memory.change import MemoryChangeRequest
from svarog_harness.memory.proposal import MemoryProposalRequest, validate_proposal
from svarog_harness.memory.writer import MemoryWriter
from svarog_harness.storage.models import MemoryProposal, MemoryProposalStatus, utcnow

_PREVIEW_LIMIT = 4_000


class MemoryProposalNotFoundError(Exception):
    def __init__(self, proposal_id: str) -> None:
        super().__init__(f"memory proposal '{proposal_id}' не найден")


class MemoryProposalStateError(Exception):
    """Proposal уже разрешён — повторное решение недопустимо."""


class MemoryProposalManager:
    def __init__(self, db: AsyncSession, memory_dir: Path) -> None:
        self._db = db
        self._memory_dir = memory_dir

    async def persist(self, request: MemoryProposalRequest) -> MemoryProposal:
        """Провалидировать и записать proposal; невалидный сохраняется как failed."""
        errors = validate_proposal(self._memory_dir, request)
        head = await GitRepo(self._memory_dir).head_sha()
        row = MemoryProposal(
            run_id=request.source_run_id,
            title=request.title.strip() or "(без названия)",
            rationale=request.rationale,
            changes=request.to_changes_json(),
            status=MemoryProposalStatus.FAILED if errors else MemoryProposalStatus.PENDING,
            memory_head=head,
            checks={"validation": errors},
        )
        self._db.add(row)
        await self._db.commit()
        return row

    async def list_pending(self, limit: int = 50) -> list[MemoryProposal]:
        result = await self._db.execute(
            select(MemoryProposal)
            .where(MemoryProposal.status == MemoryProposalStatus.PENDING)
            .order_by(MemoryProposal.created_at)
            .limit(limit)
        )
        return list(result.scalars())

    async def pending_count(self) -> int:
        result = await self._db.execute(
            select(func.count())
            .select_from(MemoryProposal)
            .where(MemoryProposal.status == MemoryProposalStatus.PENDING)
        )
        return int(result.scalar_one())

    async def get(self, proposal_id_prefix: str) -> MemoryProposal:
        result = await self._db.execute(
            select(MemoryProposal).where(MemoryProposal.id.startswith(proposal_id_prefix))
        )
        rows = list(result.scalars())
        if not rows:
            raise MemoryProposalNotFoundError(proposal_id_prefix)
        if len(rows) > 1:
            raise MemoryProposalNotFoundError(f"{proposal_id_prefix} (префикс неоднозначен)")
        return rows[0]

    async def decide(
        self,
        proposal: MemoryProposal,
        *,
        approved: bool,
        decided_by: str,
        reason: str | None = None,
    ) -> list[str]:
        """Одобрить (в очередь writer'а) или отклонить. Возвращает id заявок."""
        if proposal.status is not MemoryProposalStatus.PENDING:
            raise MemoryProposalStateError(
                f"proposal {proposal.id[:8]} уже {proposal.status.value}"
            )
        change_ids: list[str] = []
        if approved:
            writer = MemoryWriter(self._db, self._memory_dir)
            for raw in proposal.changes:
                request = MemoryChangeRequest.from_dict(
                    raw, source_run_id=proposal.run_id
                )
                row = await writer.enqueue(request)
                change_ids.append(row.id)
            proposal.status = MemoryProposalStatus.APPLIED
            proposal.applied_change_ids = change_ids
        else:
            proposal.status = MemoryProposalStatus.REJECTED
        proposal.decided_at = utcnow()
        proposal.decided_by = decided_by
        proposal.reason = reason
        await self._db.commit()
        return change_ids

    def preview(self, proposal: MemoryProposal) -> list[tuple[str, str]]:
        """Прогноз содержимого каждого файла на ТЕКУЩЕМ состоянии памяти.

        Замороженный при создании diff устарел бы: между предложением и
        одобрением память меняется. `replace_section` ищет секцию по якорю, а
        `update_field` правит одно поле — обе операции осмысленно
        переприменяются к изменившемуся файлу, поэтому пересчёт честнее снимка.
        """
        previews: list[tuple[str, str]] = []
        for raw in proposal.changes:
            request = MemoryChangeRequest.from_dict(raw)
            try:
                text = preview_content(self._memory_dir, request)
            except MemoryApplyError as exc:
                text = f"(правка больше не применима: {exc})"
            previews.append((request.file, text[:_PREVIEW_LIMIT]))
        return previews

    async def head_moved(self, proposal: MemoryProposal) -> bool:
        """Ушла ли память вперёд с момента предложения."""
        if proposal.memory_head is None:
            return False
        return await GitRepo(self._memory_dir).head_sha() != proposal.memory_head

    @staticmethod
    def validation_messages(proposal: MemoryProposal) -> list[str]:
        checks: dict[str, Any] = proposal.checks or {}
        return [str(m) for m in checks.get("validation", [])]
```

- [ ] **Step 5: Реэкспорт**

В `src/svarog_harness/memory/__init__.py` добавить в импорты и `__all__`:

```python
from svarog_harness.memory.proposal import MemoryProposalRequest, validate_proposal
from svarog_harness.memory.proposal_manager import MemoryProposalManager
from svarog_harness.memory.validate import validate_change
```

и соответственно `"MemoryProposalManager"`, `"MemoryProposalRequest"`, `"validate_change"`, `"validate_proposal"` в `__all__` (список отсортирован).

- [ ] **Step 6: Запустить тесты — должны пройти**

Run: `uv run pytest tests/test_memory_proposal_manager.py -q`
Expected: PASS (9 passed)

- [ ] **Step 7: Полные проверки и коммит**

```bash
set -e
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -q
git add src/svarog_harness/gitflow/repo.py \
        src/svarog_harness/memory/proposal_manager.py \
        src/svarog_harness/memory/__init__.py \
        tests/test_memory_proposal_manager.py
git commit -m "feat(memory): MemoryProposalManager — approve кладёт заявки в очередь writer'а"
```

---

### Task 5: Инструмент `propose_memory_change`

**Files:**
- Modify: `src/svarog_harness/tools/memory_tools.py` (добавить в конец файла)
- Test: `tests/test_memory_validate.py` (дописать раздел про инструмент)

**Interfaces:**
- Consumes: `MemoryProposalRequest`, `validate_proposal`, `MemoryChangeRequest`, `MemoryOperation`.
- Produces: `ProposeMemoryChangeTool(on_propose: Callable[[MemoryProposalRequest], None], memory_dir: Path)`; имя инструмента — `propose_memory_change`, `action_type` — `memory.propose`, риск `LOW`.

- [ ] **Step 1: Написать падающий тест**

Дописать в `tests/test_memory_validate.py`:

```python
from svarog_harness.memory.proposal import MemoryProposalRequest
from svarog_harness.tools.memory_tools import ProposeMemoryChangeTool


async def test_propose_tool_collects_request(tmp_path: Path) -> None:
    sink: list[MemoryProposalRequest] = []
    tool = ProposeMemoryChangeTool(on_propose=sink.append, memory_dir=tmp_path)

    result = await tool.execute(
        tool.args_model(
            title="дубль проектов",
            rationale="две страницы про один бот",
            changes=[
                {"file": "notes.md", "operation": "append", "content": "факт"},
            ],
        )
    )

    assert result.ok
    assert len(sink) == 1
    assert sink[0].title == "дубль проектов"
    assert sink[0].changes[0].file == "notes.md"


async def test_propose_tool_rejects_delete_of_non_empty(tmp_path: Path) -> None:
    """Правило §3 возвращается модели сразу, а не всплывает при ревью."""
    (tmp_path / "page.md").write_text("содержимое\n", encoding="utf-8")
    sink: list[MemoryProposalRequest] = []
    tool = ProposeMemoryChangeTool(on_propose=sink.append, memory_dir=tmp_path)

    result = await tool.execute(
        tool.args_model(
            title="убрать",
            rationale="лишняя",
            changes=[{"file": "page.md", "operation": "delete"}],
        )
    )

    assert not result.ok
    assert "archived" in (result.error or "")
    assert sink == []
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_memory_validate.py -q`
Expected: FAIL — `ImportError: cannot import name 'ProposeMemoryChangeTool'`

- [ ] **Step 3: Реализовать инструмент**

Дописать в конец `src/svarog_harness/tools/memory_tools.py`:

```python
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
    зарегистрирован. Один вызов = один замысел = один proposal.
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
```

Импорты в шапке модуля дополнить:

```python
from svarog_harness.memory.proposal import MemoryProposalRequest, validate_proposal
```

- [ ] **Step 4: Запустить тест — должен пройти**

Run: `uv run pytest tests/test_memory_validate.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Полные проверки и коммит**

```bash
set -e
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -q
git add src/svarog_harness/tools/memory_tools.py tests/test_memory_validate.py
git commit -m "feat(tools): propose_memory_change — замысел правки памяти на ревью"
```

---

### Task 6: `RunProfile` и урезанный реестр Dream

**Files:**
- Modify: `src/svarog_harness/runtime/orchestrator.py` (`_build_registry` ~566-633, `build_loop` ~372-448, `run_once` ~837-979, новый `drain_memory_proposals` рядом с `drain_proposals` ~1272)
- Test: `tests/test_dream_profile.py`

**Interfaces:**
- Consumes: `ProposeMemoryChangeTool` (Task 5), `MemoryProposalManager` (Task 4).
- Produces:
  - `RunProfile` (StrEnum: `DEFAULT = "default"`, `DREAM = "dream"`) в `runtime/orchestrator.py`;
  - `TaskRunner.run_once(..., profile: RunProfile = RunProfile.DEFAULT)`;
  - `TaskRunner.drain_memory_proposals(db, sink, run_id, hooks) -> None`;
  - `RunHooks.on_memory_proposal: Callable[[MemoryProposal], None] | None`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_dream_profile.py`:

```python
"""Тесты профиля Dream (блок C §6): реестр инструментов урезан структурно."""

from pathlib import Path

from svarog_harness.runtime.orchestrator import RunProfile

# Инструменты, которых у Dream быть не должно. Проверяем поимённо, а не по
# количеству: иначе тест сломается от каждого нового инструмента в проекте.
FORBIDDEN = ("remember", "bash", "write_file", "edit_file", "spawn_child_run", "update_plan")


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
```

Фикстуры `dream_registry_names` и `default_registry_names` строят реестр через `TaskRunner._build_registry` с минимальным окружением. Добавить в тот же файл:

```python
import pytest

from svarog_harness.config.schema import SvarogConfig
from svarog_harness.runtime.orchestrator import TaskRunner


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
    from svarog_harness.sandbox.local import LocalEnvironment  # локальный импорт: тяжёлый модуль

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
```

Если класс локального окружения называется иначе — найти его так: `grep -rn "class .*Environment" src/svarog_harness/sandbox/` и подставить фактическое имя и сигнатуру конструктора. Реестру окружение нужно только для `BashTool`, который в профиле `DREAM` не создаётся.

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_dream_profile.py -q`
Expected: FAIL — `ImportError: cannot import name 'RunProfile'`

- [ ] **Step 3: Ввести `RunProfile` и ветвление реестра**

В `src/svarog_harness/runtime/orchestrator.py` рядом с другими enum'ами модуля добавить:

```python
class RunProfile(StrEnum):
    """Набор инструментов, доступный run'у.

    DREAM — единственный run, который запускается без человека И обрабатывает
    содержимое, попавшее в память из внешних источников (sources/, заметки
    прошлых run'ов). Дать ему shell и файловые tools означало бы, что текст в
    памяти управляет исполнением; профиль закрывает это структурно, а не
    настройкой, которую можно перепутать.
    """

    DEFAULT = "default"
    DREAM = "dream"
```

(`StrEnum` импортируется из `enum`.)

Сигнатуру `_build_registry` дополнить двумя keyword-параметрами:

```python
        memory_proposal_sink: list[MemoryProposalRequest] | None = None,
        profile: RunProfile = RunProfile.DEFAULT,
```

и в начале тела метода, сразу после `registry = ToolRegistry()`, добавить ранний выход:

```python
        registry = ToolRegistry()
        if profile is RunProfile.DREAM:
            # Только чтение памяти и предложение правок; всё остальное — включая
            # remember — не регистрируется вовсе (§6 спеки блока C).
            if mem_dir is not None:
                registry.register(ReadMemoryTool(mem_dir))
                if memory_proposal_sink is not None:
                    registry.register(
                        ProposeMemoryChangeTool(
                            on_propose=memory_proposal_sink.append, memory_dir=mem_dir
                        )
                    )
            return registry
```

Остальное тело метода остаётся без изменений.

- [ ] **Step 4: Прокинуть профиль через `build_loop` и `run_once`**

В `build_loop` добавить keyword-параметры `memory_proposal_sink: list[MemoryProposalRequest] | None = None` и `profile: RunProfile = RunProfile.DEFAULT`, передав их в вызов `self._build_registry` (~строка 414).

В `run_once` добавить keyword-параметр `profile: RunProfile = RunProfile.DEFAULT`; внутри `action` завести сток и прокинуть профиль:

```python
                memory_proposal_sink: list[MemoryProposalRequest] = []
```

рядом с `proposal_sink` (~строка 912), передать его и `profile` в `self.build_loop(...)` (~строка 956), и после `drain_proposals` добавить:

```python
                await self.drain_memory_proposals(
                    db, memory_proposal_sink, outcome.run_id, hooks
                )
```

- [ ] **Step 5: Добавить `drain_memory_proposals` и хук**

Рядом с `drain_proposals` (~строка 1272) добавить:

```python
    async def drain_memory_proposals(
        self,
        db: AsyncSession,
        sink: list[MemoryProposalRequest],
        run_id: str,
        hooks: RunHooks,
    ) -> None:
        """Материализовать предложения правок памяти (блок C, ADR-0020)."""
        if not sink:
            return
        mem_dir = memory_dir(self._cfg)
        if mem_dir is None or not mem_dir.is_dir():
            return
        manager = MemoryProposalManager(db, mem_dir)
        for request in sink:
            row = await manager.persist(replace(request, source_run_id=run_id))
            if hooks.on_memory_proposal is not None:
                hooks.on_memory_proposal(row)
```

В `RunHooks` (~строка 121, рядом с `on_proposal`) добавить поле:

```python
    on_memory_proposal: Callable[[MemoryProposal], None] | None = None
```

Импорты модуля дополнить: `MemoryProposalRequest`, `MemoryProposalManager` из `svarog_harness.memory`, `MemoryProposal` из `svarog_harness.storage.models`, `StrEnum` из `enum`.

- [ ] **Step 6: Запустить тесты — должны пройти**

Run: `uv run pytest tests/test_dream_profile.py -q`
Expected: PASS (4 passed)

- [ ] **Step 7: Убедиться, что обычные run'ы не сломались**

Run: `uv run pytest tests/test_loop.py tests/test_memory.py tests/test_orchestrator.py -q`
Expected: PASS. Если файла `tests/test_orchestrator.py` нет — прогнать `uv run pytest -q -k orchestrator`.

- [ ] **Step 8: Полные проверки и коммит**

```bash
set -e
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -q
git add src/svarog_harness/runtime/orchestrator.py tests/test_dream_profile.py
git commit -m "feat(runtime): RunProfile.DREAM — реестр без remember, shell и файловых tools"
```

---

### Task 7: `DreamConfig`

**Files:**
- Modify: `src/svarog_harness/config/schema.py` (после `SchedulerConfig`, ~строка 271; поле в `SvarogConfig` ~строка 465)
- Test: `tests/test_config.py` (дописать; если файла нет — создать `tests/test_dream_config.py` с теми же тестами)

**Interfaces:**
- Produces: `DreamConfig(enabled: bool = False, interval_sec: int = 86_400, max_pending: int = 20, max_iterations: int = 20)`; поле `SvarogConfig.dream`.

- [ ] **Step 1: Написать падающий тест**

```python
def test_dream_is_opt_in_by_default() -> None:
    """Механизм опинионейтед и стоит денег — как curator.semantic (ADR-0009)."""
    assert DreamConfig().enabled is False


def test_dream_defaults() -> None:
    cfg = DreamConfig()
    assert cfg.interval_sec == 86_400
    assert cfg.max_pending == 20
    assert cfg.max_iterations == 20


def test_dream_rejects_unknown_key() -> None:
    """StrictModel: опечатка в имени поля — ошибка загрузки, а не тихий дефолт."""
    with pytest.raises(ValidationError):
        DreamConfig.model_validate({"enabled": True, "enabeld": True})
```

Импорты: `from svarog_harness.config.schema import DreamConfig`, `from pydantic import ValidationError`, `import pytest`.

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest -q -k dream`
Expected: FAIL — `ImportError: cannot import name 'DreamConfig'`

- [ ] **Step 3: Реализовать**

В `src/svarog_harness/config/schema.py` после `SchedulerConfig`:

```python
class DreamConfig(StrictModel):
    """Dream — семантический слой памяти (блок C, ADR-0020).

    Выключен по умолчанию: механизм опинионейтед и тратит токены, поэтому
    opt-in — как слой 2 skill-curator'а (ADR-0009). Конфиг служит гейтом при
    заводке системной джобы; после заводки джоба управляется через
    `svarog cron enable|disable`, и конфиг её больше не переключает.
    """

    enabled: bool = False
    interval_sec: int = Field(default=86_400, gt=0)
    # Потолок непросмотренных предложений: без него ежедневная джоба при
    # неактивном человеке копит мусор без границы и платно.
    max_pending: int = Field(default=20, gt=0)
    max_iterations: int = Field(default=20, gt=0)
```

В `SvarogConfig` рядом с `scheduler`:

```python
    dream: DreamConfig = Field(default_factory=DreamConfig)
```

- [ ] **Step 4: Запустить тест — должен пройти**

Run: `uv run pytest -q -k dream`
Expected: PASS

- [ ] **Step 5: Полные проверки и коммит**

```bash
set -e
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -q
git add src/svarog_harness/config/schema.py tests/
git commit -m "feat(config): DreamConfig — opt-in семантический слой памяти"
```

---

### Task 8: Задача Dream и системная джоба

**Files:**
- Create: `src/svarog_harness/memory/dream.py`
- Modify: `src/svarog_harness/scheduler/system_jobs.py`
- Test: `tests/test_dream_profile.py` (дописать), `tests/test_scheduler_store.py` или новый раздел в `tests/test_dream_profile.py` для заводки джобы

**Interfaces:**
- Consumes: `MemoryAuditReport`, `MemoryFinding` из `memory/curator.py`.
- Produces:
  - `build_dream_task(report: MemoryAuditReport) -> str`;
  - `DREAM_JOB_NAME = "system:memory-dream"` в `scheduler/system_jobs.py`;
  - `ensure_system_jobs(..., dream_enabled: bool, dream_interval_sec: int)`.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `tests/test_dream_profile.py`:

```python
from svarog_harness.memory.curator import MemoryAuditReport, MemoryFinding
from svarog_harness.memory.dream import build_dream_task


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
```

И тесты заводки джобы (в том же файле, отдельным разделом):

```python
from datetime import UTC, datetime

from svarog_harness.scheduler.store import JobStore
from svarog_harness.scheduler.system_jobs import DREAM_JOB_NAME, ensure_system_jobs

_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC).replace(tzinfo=None)


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
```

Фикстуру `db` скопировать из `tests/test_memory_proposal_manager.py` (Task 1) — или, если удобнее, вынести её в `tests/conftest.py` и убрать дубликат из обоих файлов.

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_dream_profile.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.memory.dream'`

- [ ] **Step 3: Реализовать текст задачи**

Создать `src/svarog_harness/memory/dream.py`:

```python
"""Dream — семантический слой памяти (блок C, ADR-0020).

Структурный аудит прогоняется кодом ДО модели, и его находки подаются в задачу
как факты: перепроверять детерминированный результат языковой моделью — трата
токенов и источник разногласий. Модель занимается тем, чего аудит не умеет:
дублями и противоречиями по смыслу.
"""

from svarog_harness.memory.curator import MemoryAuditReport

_INTRO = """Ты — фоновый процесс консолидации долговременной памяти агента.

Ты НЕ можешь писать в память напрямую. Единственный способ что-то изменить —
инструмент `propose_memory_change`: он оформляет предложение, которое применит
человек после ревью. Страницы читай через `read_memory`; список всех страниц —
в index.md, он уже в контексте.

Правила оформления предложений:
* один вызов `propose_memory_change` = один связный замысел; несвязанные правки
  оформляй отдельными вызовами, человек решает по каждому отдельно;
* `rationale` пиши так, чтобы он был понятен без контекста этого прогона;
* удалять непустые страницы нельзя. Проект, который закончился, переводи в
  `status: archived` через операцию `update_field` — страница остаётся."""

_STRUCTURAL_HEADER = """
Структурный аудит памяти уже выполнен кодом. Это установленные факты, их
перепроверять не нужно — нужно предложить починку:"""

_STRUCTURAL_EMPTY = """
Структурный аудит памяти уже выполнен кодом: находок нет, структура в порядке."""

_SEMANTIC = """
Далее сделай смысловой проход, которого детерминированный аудит не умеет:
* два проекта, описывающие одно и то же — предложи слияние;
* взаимно противоречащие утверждения на разных страницах — предложи, какое
  оставить, и объясни в rationale, почему;
* устаревшие формулировки, которые опровергаются более свежими страницами.

Если по итогам прохода предлагать нечего — так и напиши в финальном ответе.
Пустое предложение хуже отсутствия предложения."""


def build_dream_task(report: MemoryAuditReport) -> str:
    """Собрать текст задачи прогона Dream из находок структурного аудита."""
    parts = [_INTRO]
    if report.findings:
        parts.append(_STRUCTURAL_HEADER)
        parts.extend(
            f"* {finding.kind}: {finding.path} — {finding.detail}" for finding in report.findings
        )
    else:
        parts.append(_STRUCTURAL_EMPTY)
    parts.append(_SEMANTIC)
    return "\n".join(parts)
```

- [ ] **Step 4: Завести системную джобу под гейтом**

В `src/svarog_harness/scheduler/system_jobs.py` добавить рядом с `CURATOR_JOB_NAME`:

```python
DREAM_JOB_NAME = "system:memory-dream"

# Текст задачи Dream собирается в момент запуска из находок аудита
# (memory/dream.py), поэтому в джобе лежит только маркер: диспетчер в CLI
# узнаёт Dream по имени и подставляет актуальную задачу.
_DREAM_TASK = "Консолидация долговременной памяти (Dream, ADR-0020)."
```

Сигнатуру `ensure_system_jobs` дополнить keyword-параметрами `dream_enabled: bool` и `dream_interval_sec: int`, а в конец тела, перед `return created`, добавить:

```python
    if dream_enabled and DREAM_JOB_NAME not in existing:
        spec = str(dream_interval_sec)
        job = await store.create(
            name=DREAM_JOB_NAME,
            kind=ScheduleKind.EVERY,
            spec=spec,
            tz="UTC",
            task=_DREAM_TASK,
            workspace=workspace,
            autonomy=autonomy,
            config_digest=config_digest,
            origin=JobOrigin.SYSTEM,
            first_run_at=next_run_after(ScheduleKind.EVERY, spec, "UTC", now),
            protected=True,
        )
        await store.set_enabled(job, True)
        created.append(job.id)
```

Дополнить докстринг модуля: конфиг `dream.enabled` — гейт **на заводке**; выключение уже заведённой джобы делается через `svarog cron disable`, и повторный старт демона её не включает обратно.

- [ ] **Step 5: Запустить тесты — должны пройти**

Run: `uv run pytest tests/test_dream_profile.py -q`
Expected: PASS (9 passed)

- [ ] **Step 6: Проверить, что старые тесты планировщика не сломались**

Run: `uv run pytest tests/test_scheduler_store.py tests/test_scheduler_tick.py tests/test_cli_scheduler.py -q`
Expected: PASS. Вызовы `ensure_system_jobs` в них придётся дополнить новыми аргументами — сделать это в этом же шаге.

- [ ] **Step 7: Полные проверки и коммит**

```bash
set -e
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -q
git add src/svarog_harness/memory/dream.py \
        src/svarog_harness/scheduler/system_jobs.py \
        tests/
git commit -m "feat(dream): задача из находок аудита и системная джоба под гейтом dream.enabled"
```

---

### Task 9: CLI — ревью и запуск Dream

**Files:**
- Modify: `src/svarog_harness/cli/main.py` (`memory_app` ~2026; `_scheduler_loop`/`run_job` ~1935-1999; `on_proposal`-соседство ~679)
- Test: `tests/test_cli_memory_proposals.py`

**Interfaces:**
- Consumes: `MemoryProposalManager`, `build_dream_task`, `audit_memory`, `RunProfile`, `DREAM_JOB_NAME`.
- Produces: команды `svarog memory proposals list|show|approve|reject`; диспетчеризация профиля в `run_job`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_cli_memory_proposals.py`:

```python
"""Тесты CLI-ревью memory proposals (блок C §5)."""

from pathlib import Path

from typer.testing import CliRunner

from svarog_harness.cli.main import app

runner = CliRunner()


def test_proposals_list_on_empty_db(svarog_home: Path) -> None:
    result = runner.invoke(app, ["memory", "proposals", "list"])
    assert result.exit_code == 0
    assert "ожидающих" in result.stdout


def test_show_unknown_id_exits_with_error(svarog_home: Path) -> None:
    result = runner.invoke(app, ["memory", "proposals", "show", "deadbeef"])
    assert result.exit_code == 1
    assert "не найден" in result.stdout
```

Фикстура `svarog_home` должна поднять временный `svarog.yaml` с `memory.path` и `storage.db_path` внутри `tmp_path` и сделать его текущим каталогом. Взять готовый образец из `tests/test_cli_scheduler.py` — там та же задача уже решена; при отличии имени фикстуры использовать тамошнее.

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_cli_memory_proposals.py -q`
Expected: FAIL — `No such command 'proposals'`

- [ ] **Step 3: Добавить команды ревью**

В `src/svarog_harness/cli/main.py` после `memory_curate` (~строка 2103):

```python
memory_proposals_app = typer.Typer(
    help="Memory proposals (блок C): ревью правок памяти, предложенных Dream.",
    no_args_is_help=True,
)
memory_app.add_typer(memory_proposals_app, name="proposals")


def _memory_dir_or_exit(cfg: SvarogConfig) -> Path:
    mem_dir = memory_dir(cfg)
    if mem_dir is None or not mem_dir.is_dir():
        console.print("память не настроена или каталог отсутствует")
        raise typer.Exit(code=1)
    return mem_dir


@memory_proposals_app.command("list")
def memory_proposals_list() -> None:
    """Показать предложения правок памяти, ожидающие ревью."""
    cfg = _load_config_or_exit()
    mem_dir = _memory_dir_or_exit(cfg)

    async def action(db: AsyncSession) -> None:
        rows = await MemoryProposalManager(db, mem_dir).list_pending()
        if not rows:
            console.print("ожидающих memory proposals нет")
            return
        for row in rows:
            console.print(
                f"[cyan]{row.id[:8]}[/cyan] {row.title} "
                f"({len(row.changes)} правок, {row.origin.value})"
            )
        console.print(
            "[dim]review: svarog memory proposals show <id> → approve/reject <id>[/dim]"
        )

    asyncio.run(_with_db(cfg, action))


@memory_proposals_app.command("show")
def memory_proposals_show(
    proposal_id: Annotated[str, typer.Argument(help="id proposal'а или его префикс")],
) -> None:
    """Показать замысел, обоснование и предпросмотр каждой правки."""
    cfg = _load_config_or_exit()
    mem_dir = _memory_dir_or_exit(cfg)

    async def action(db: AsyncSession) -> None:
        manager = MemoryProposalManager(db, mem_dir)
        try:
            row = await manager.get(proposal_id)
        except MemoryProposalNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        console.print(f"[bold]{row.title}[/bold] | {row.status.value} | {row.id[:8]}")
        console.print(f"  обоснование: {row.rationale}")
        for message in MemoryProposalManager.validation_messages(row):
            console.print(f"  [yellow]{message}[/yellow]")
        if await manager.head_moved(row):
            console.print(
                "[yellow]память изменилась с момента предложения — "
                "предпросмотр ниже посчитан на текущем состоянии[/yellow]"
            )
        for path, preview in manager.preview(row):
            console.print(f"\n[bold]{path}[/bold]")
            console.print(preview)

    asyncio.run(_with_db(cfg, action))


def _decide_memory_proposal(proposal_id: str, *, approved: bool, reason: str | None) -> None:
    cfg = _load_config_or_exit()
    mem_dir = _memory_dir_or_exit(cfg)

    async def action(db: AsyncSession) -> tuple[str, int]:
        manager = MemoryProposalManager(db, mem_dir)
        row = await manager.get(proposal_id)
        ids = await manager.decide(
            row, approved=approved, decided_by="cli", reason=reason
        )
        return row.id, len(ids)

    try:
        row_id, count = asyncio.run(_with_db(cfg, action))
    except MemoryProposalNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    except MemoryProposalStateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    if approved:
        console.print(
            f"[green]proposal {row_id[:8]} одобрен[/green]: {count} заявок в очереди; "
            f"применить сейчас — svarog memory flush"
        )
    else:
        console.print(f"[yellow]proposal {row_id[:8]} отклонён[/yellow]")


@memory_proposals_app.command("approve")
def memory_proposals_approve(
    proposal_id: Annotated[str, typer.Argument(help="id proposal'а или его префикс")],
    reason: Annotated[str | None, typer.Option("--reason", help="Комментарий")] = None,
) -> None:
    """Одобрить предложение: заявки уходят в очередь единственного писателя."""
    _decide_memory_proposal(proposal_id, approved=True, reason=reason)


@memory_proposals_app.command("reject")
def memory_proposals_reject(
    proposal_id: Annotated[str, typer.Argument(help="id proposal'а или его префикс")],
    reason: Annotated[str | None, typer.Option("--reason", help="Причина отказа")] = None,
) -> None:
    """Отклонить предложение. Память не меняется."""
    _decide_memory_proposal(proposal_id, approved=False, reason=reason)
```

Импорты в шапке файла дополнить:

```python
from svarog_harness.memory.proposal_manager import (
    MemoryProposalManager,
    MemoryProposalNotFoundError,
    MemoryProposalStateError,
)
```

- [ ] **Step 4: Прокинуть новые аргументы в `ensure_system_jobs`**

В `_scheduler_loop` (~строка 1946) дополнить вызов:

```python
            prune_interval_sec=cfg.curator.prune_interval_sec,
            dream_enabled=cfg.dream.enabled,
            dream_interval_sec=cfg.dream.interval_sec,
```

- [ ] **Step 5: Диспетчеризация профиля в `run_job`**

Заменить тело `run_job` (~строка 1959) на версию с ветвлением:

```python
    async def run_job(request: JobRunRequest) -> str:
        task, profile = request.task, RunProfile.DEFAULT
        if request.name == DREAM_JOB_NAME:
            blocked = await _with_db(cfg, _dream_blocked)
            if blocked is not None:
                return blocked
            mem_dir = memory_dir(cfg)
            assert mem_dir is not None  # проверено в _dream_blocked
            report = audit_memory(mem_dir, stale_after_days=cfg.curator.stale_after_days)
            task, profile = build_dream_task(report), RunProfile.DREAM

        runner = TaskRunner(cfg, Path(request.workspace))
        outcome = await runner.run_once(
            task, AutonomyMode(request.autonomy), hooks=RunHooks(), profile=profile
        )
        ...
```

`JobRunRequest` полем `name` не располагает, поэтому его надо добавить: в `src/svarog_harness/scheduler/ticker.py` дописать `name: str` в датакласс и заполнить его в `tick` (`name=job.name`). Это изменение затрагивает `tests/test_scheduler_tick.py` — поправить конструкторы `JobRunRequest` там же.

Функция-гейт `_dream_blocked` объявляется рядом с `_scheduler_loop`:

```python
async def _dream_blocked(cfg: SvarogConfig, db: AsyncSession) -> str | None:
    """Причина не запускать Dream сейчас, или None.

    Потолок непросмотренных предложений — предохранитель от бесконечного
    накопления: без него ежедневная джоба при неактивном человеке копит мусор
    и тратит токены впустую.
    """
    mem_dir = memory_dir(cfg)
    if mem_dir is None or not mem_dir.is_dir():
        return "пропущено: память не настроена"
    pending = await MemoryProposalManager(db, mem_dir).pending_count()
    if pending >= cfg.dream.max_pending:
        return f"пропущено: {pending} непросмотренных предложений — сначала ревью"
    return None
```

Поскольку `_with_db` передаёт в колбэк только сессию, обернуть частичным применением:

```python
            blocked = await _with_db(cfg, lambda db: _dream_blocked(cfg, db))
```

- [ ] **Step 6: Уведомление в чате**

Рядом с `on_proposal` (~строка 679) добавить обработчик и передать его в `RunHooks(..., on_memory_proposal=on_memory_proposal)` в том же месте, где передаётся `on_proposal` (~строка 724):

```python
    def on_memory_proposal(proposal: MemoryProposal) -> None:
        console.print(
            f"[cyan]memory proposal[/cyan] {proposal.title} "
            f"[dim](review: svarog memory proposals show {proposal.id[:8]})[/dim]"
        )
```

- [ ] **Step 7: Запустить тесты — должны пройти**

Run: `uv run pytest tests/test_cli_memory_proposals.py tests/test_scheduler_tick.py tests/test_cli_scheduler.py -q`
Expected: PASS

- [ ] **Step 8: Ручная проверка сквозного пути**

```bash
uv run svarog memory proposals list
```
Expected: `ожидающих memory proposals нет` и код возврата 0. Если память не настроена — сообщение про память и код 1; это тоже корректный исход, тогда проверить с настроенным `memory.path`.

- [ ] **Step 9: Полные проверки и коммит**

```bash
set -e
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -q
git add src/svarog_harness/cli/main.py \
        src/svarog_harness/scheduler/ticker.py \
        tests/
git commit -m "feat(cli): ревью memory proposals и запуск Dream из системной джобы"
```

---

### Task 10: Документы

**Files:**
- Create: `docs/adr/0020-memory-proposals-and-dream.md`
- Modify: `docs/adr/0011-memory-wiki-progressive-index.md` (раздел «Lint — memory-curator», абзац «Отложено»)
- Modify: `docs/adr/0019-scheduler.md` (упоминание второго потребителя системных джоб)
- Modify: `docs/reference-analysis.md:114-126` (блок C → «перенесено»)
- Modify: `README.md` (раздел про память — команды ревью)

- [ ] **Step 1: Написать ADR-0020**

Создать `docs/adr/0020-memory-proposals-and-dream.md` по образцу соседних ADR (Статус / Контекст / Решение / Последствия). Содержание — сжатый пересказ спеки: почему очередь, а не ветка; почему ревью по автору, а не по операции; почему delete запрещён для непустых страниц; почему у Dream урезанный реестр; почему конфиг — гейт только на заводке. Статус: «Принято».

- [ ] **Step 2: Закрыть отложенный шаг в ADR-0011**

В разделе «Lint — memory-curator» заменить абзац «Отложено (нужна отдельная подсистема)» на констатацию: семантический слой и memory-proposals реализованы (ADR-0020); за структурным аудитом остаётся роль поставщика фактов для Dream.

- [ ] **Step 3: Обновить ADR-0019**

Добавить, что системных джоб теперь две: `system:skill-curator` и `system:memory-dream`, причём вторая заводится под гейтом конфига и после заводки управляется как обычная джоба.

- [ ] **Step 4: Обновить reference-analysis**

Раздел `### 3.2. Что отложено (блок C, не в этом цикле)` переименовать и переписать: блок C перенесён (ссылка на спеку и ADR-0020). Убедиться, что список отложенного не остался пустым разделом с заголовком «что отложено» — либо переформулировать заголовок, либо удалить раздел, перенеся содержимое в §3.1.

- [ ] **Step 5: Обновить README**

В разделе про память добавить: Dream выключен по умолчанию, включается `dream.enabled: true`, ревью — `svarog memory proposals list|show|approve|reject`, применение одобренного — при следующем `svarog memory flush` или автоматически после следующего run'а.

- [ ] **Step 6: Проверки и коммит**

```bash
set -e
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -q
git add docs/ README.md
git commit -m "docs(adr): ADR-0020 memory-proposals и Dream; блок C перенесён"
```

---

### Task 11: Сценарий симуляции

**Files:**
- Modify: `simulation/scenarios.md`

Юниты не проверяют качество семантического прохода — это делает сценарий с живой моделью.

- [ ] **Step 1: Добавить сценарий S21**

Дописать в `simulation/scenarios.md` по формату соседних сценариев:

* **Подготовка:** временный каталог памяти с двумя страницами проектов, описывающими один и тот же бот под разными slug'ами, плюс страница `decisions/`, противоречащая одному из overview, плюс пустой файл и папка проекта без `overview.md`.
* **Конфигурация:** `dream.enabled: true`, `dream.max_pending: 20`.
* **Действие:** запустить прогон Dream (профиль `DREAM`) один раз.
* **Ожидания:** созданы отдельные proposal'ы на дубль и на противоречие; у каждого непустой `rationale`; ни один proposal не содержит `delete` непустой страницы; находки структурного аудита (`orphan`, `empty`) отражены в предложениях; память на диске **не изменилась** до approve.
* **Границы:** только временный каталог, никогда против реального `agent-home/memory`.

- [ ] **Step 2: Прогнать сценарий**

Запустить симуляцию согласно инструкции в `simulation/`. Живые вызовы платные — прогонять один раз, результат (дата, конфигурация, исход) зафиксировать в описании сценария.

- [ ] **Step 3: Коммит**

```bash
git add simulation/scenarios.md
git commit -m "test(simulation): S21 — семантический проход Dream на подготовленной памяти"
```

---

## Самопроверка плана

**Покрытие спеки:**

| раздел спеки | задача |
|---|---|
| Границы: ревью по автору | Task 6 (профиль без `remember`), Task 5 (инструмент) |
| §1 модель данных | Task 1 |
| §2 инструмент + общая валидация | Task 2, Task 5 |
| §3 delete-правило и потолок pending | Task 3 (delete), Task 9 (`_dream_blocked`) |
| §4 применение, отклонение, устаревание | Task 4 (`decide`, `preview`, `head_moved`) |
| §5 CLI | Task 9 |
| §6 профиль run'а | Task 6 |
| §7 системная джоба и выключатель | Task 8, Task 9 (проброс конфига) |
| §8 конфигурация | Task 7 |
| §9 таблица изменений | покрыта Tasks 1-9 целиком, включая `gitflow/repo.py` (Task 4) |
| §10 тестирование | Tasks 1-9 (юниты), Task 11 (симуляция) |

**Согласованность имён:** `validate_change` (Tasks 2, 3), `validate_proposal` (Tasks 3, 5), `MemoryProposalRequest` (Tasks 3, 4, 5, 6), `MemoryProposalManager.persist/decide/preview/head_moved/pending_count` (Tasks 4, 9), `RunProfile.DREAM` (Tasks 6, 9), `DREAM_JOB_NAME` (Tasks 8, 9), `build_dream_task` (Tasks 8, 9), `head_sha` (Task 4). Расхождений нет.

**Известные точки, где план опирается на факт, который исполнитель обязан проверить сам:**

1. Имя и конструктор класса локального окружения в `tests/test_dream_profile.py` (Task 6, Step 1) — план даёт команду для поиска.
2. Имя фикстуры временного дома в CLI-тестах (Task 9, Step 1) — план указывает файл-образец.
3. Наличие теста, сверяющего `Base.metadata` с миграциями (Task 1, Step 6) — если такого теста нет, шаг проходит вхолостую, это нормально.
