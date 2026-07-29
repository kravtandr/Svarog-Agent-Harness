"""Тесты общей валидации заявки памяти (блок C): один свод правил на оба пути записи."""

from pathlib import Path

from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.proposal import MemoryProposalRequest
from svarog_harness.memory.validate import validate_change
from svarog_harness.tools.memory_tools import ProposeMemoryChangeTool


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


def test_pending_change_relaxes_existence_check(tmp_path: Path) -> None:
    """Файл, поставленный в очередь этим же run'ом, ещё не на диске — не ошибка."""
    target = str((tmp_path / "notes.md").resolve())
    change = _req("notes.md", MemoryOperation.UPDATE_FIELD, field="status", content="active")
    error = validate_change(
        tmp_path,
        change,
        pending_changes={target: [change]},
    )
    assert error is None


def test_update_field_composes_queued_summary_then_status(tmp_path: Path) -> None:
    """Regression S8: цепочка update_field summary → update_field status на
    странице без summary. Вторая заявка валидируется по суммированному
    состоянию (summary уже добавлен первой), а не по диску — иначе падает
    «нет обязательных полей: summary»."""
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "x").mkdir()
    page = tmp_path / "projects" / "x" / "overview.md"
    page.write_text(
        "---\nname: x\nslug: x\nstatus: active\n---\n## Тело\nрешение\n", encoding="utf-8"
    )
    target = str(page.resolve())
    add_summary = _req(
        "projects/x/overview.md", MemoryOperation.UPDATE_FIELD, field="summary", content="бот"
    )
    set_status = _req(
        "projects/x/overview.md", MemoryOperation.UPDATE_FIELD, field="status", content="paused"
    )
    error = validate_change(tmp_path, set_status, pending_changes={target: [add_summary]})
    assert error is None, error


def test_update_field_still_requires_summary_without_queue(tmp_path: Path) -> None:
    """Composition не ослабила контракт: одиночный update_field status на
    странице без summary (без queued summary) всё равно падает."""
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "x").mkdir()
    page = tmp_path / "projects" / "x" / "overview.md"
    page.write_text(
        "---\nname: x\nslug: x\nstatus: active\n---\n## Тело\nрешение\n", encoding="utf-8"
    )
    error = validate_change(
        tmp_path,
        _req(
            "projects/x/overview.md",
            MemoryOperation.UPDATE_FIELD,
            field="status",
            content="paused",
        ),
    )
    assert error is not None and "summary" in error


def test_valid_append_passes(tmp_path: Path) -> None:
    assert validate_change(tmp_path, _req("notes.md", MemoryOperation.APPEND, content="x")) is None


def test_redundant_memory_prefix_rejected(tmp_path: Path) -> None:
    # Находка симуляции S30: слабая модель дописывает лишний префикс memory/
    # (memory/user/profile.md), путь внутри jail → создавался осиротевший
    # вложенный файл. Отклоняем с подсказкой, модель повторяет верно.
    error = validate_change(
        tmp_path, _req("memory/user/profile.md", MemoryOperation.APPEND, content="x")
    )
    assert error is not None and "memory/" in error


def test_sources_path_not_falsely_flagged_as_memory_prefix(tmp_path: Path) -> None:
    # Только первый сегмент 'memory' — ошибка; 'memories/...' или обычные пути нет.
    req = _req("memories.md", MemoryOperation.APPEND, content="x")
    assert validate_change(tmp_path, req) is None


# --- инструмент propose_memory_change (блок C §2) ----------------------------


async def test_propose_tool_collects_request(tmp_path: Path) -> None:
    sink: list[MemoryProposalRequest] = []
    tool = ProposeMemoryChangeTool(on_propose=sink.append, memory_dir=tmp_path)

    result = await tool.execute(
        tool.args_model(
            title="дубль проектов",
            rationale="две страницы про один бот",
            changes=[{"file": "notes.md", "operation": "append", "content": "факт"}],
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


async def test_propose_tool_requires_rationale(tmp_path: Path) -> None:
    sink: list[MemoryProposalRequest] = []
    tool = ProposeMemoryChangeTool(on_propose=sink.append, memory_dir=tmp_path)

    result = await tool.execute(
        tool.args_model(
            title="правка",
            rationale="  ",
            changes=[{"file": "notes.md", "operation": "append", "content": "факт"}],
        )
    )

    assert not result.ok
    assert sink == []
