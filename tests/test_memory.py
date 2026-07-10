"""Тесты памяти Flow A (§6.7, ADR-0004): apply, reader, single writer, git-коммиты."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.memory.apply import MemoryApplyError, apply_change
from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.reader import read_memory
from svarog_harness.memory.writer import MemoryWriter
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import MemoryChange, MemoryChangeStatus
from svarog_harness.tools.memory_tools import ReadMemoryTool, RememberTool
from svarog_harness.trace.recorder import TraceRecorder


async def _make_run(db: AsyncSession, task: str) -> str:
    """Создать реальный Run (source_run_id — FK на runs.id)."""
    run = await TraceRecorder(db).start_run(task=task, autonomy="yolo", model="test")
    return run.id


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


# --- apply_change ---


def test_apply_create_and_append(tmp_path: Path) -> None:
    apply_change(
        tmp_path,
        MemoryChangeRequest(
            file="user/profile.md", operation=MemoryOperation.CREATE, content="# Профиль\n"
        ),
    )
    assert (tmp_path / "user/profile.md").read_text(encoding="utf-8") == "# Профиль\n"

    apply_change(
        tmp_path,
        MemoryChangeRequest(
            file="user/profile.md", operation=MemoryOperation.APPEND, content="любит краткость\n"
        ),
    )
    text = (tmp_path / "user/profile.md").read_text(encoding="utf-8")
    assert "любит краткость" in text


def test_apply_replace_section(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text(
        "# Заметки\n\n## Проект\nстарое\n\n## Прочее\nхвост\n", encoding="utf-8"
    )
    apply_change(
        tmp_path,
        MemoryChangeRequest(
            file="notes.md",
            operation=MemoryOperation.REPLACE_SECTION,
            section="Проект",
            content="новое содержимое",
        ),
    )
    text = (tmp_path / "notes.md").read_text(encoding="utf-8")
    assert "новое содержимое" in text
    assert "старое" not in text
    assert "## Прочее" in text  # соседняя секция цела
    assert "хвост" in text


def test_apply_replace_missing_section(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("# Заметки\n", encoding="utf-8")
    with pytest.raises(MemoryApplyError, match="секция"):
        apply_change(
            tmp_path,
            MemoryChangeRequest(
                file="notes.md",
                operation=MemoryOperation.REPLACE_SECTION,
                section="Нет",
                content="x",
            ),
        )


def test_apply_update_field(tmp_path: Path) -> None:
    (tmp_path / "page.md").write_text(
        "---\nname: X\nstatus: active\n---\n\n## Тело\nтекст\n", encoding="utf-8"
    )
    apply_change(
        tmp_path,
        MemoryChangeRequest(
            file="page.md",
            operation=MemoryOperation.UPDATE_FIELD,
            field="status",
            content="paused",
        ),
    )
    text = (tmp_path / "page.md").read_text(encoding="utf-8")
    assert "status: paused" in text
    assert "status: active" not in text
    assert "name: X" in text  # прочие поля целы
    assert "## Тело" in text and "текст" in text  # тело цело


def test_apply_update_field_stamps_updated_on_project_page(tmp_path: Path) -> None:
    from datetime import date

    (tmp_path / "projects" / "foo").mkdir(parents=True)
    (tmp_path / "projects" / "foo" / "overview.md").write_text(
        "---\nname: Foo\nslug: foo\nsummary: s\nstatus: active\n"
        "created: 2026-01-01\nupdated: 2026-01-01\n---\n\n## Описание\nтекст\n",
        encoding="utf-8",
    )
    apply_change(
        tmp_path,
        MemoryChangeRequest(
            file="projects/foo/overview.md",
            operation=MemoryOperation.UPDATE_FIELD,
            field="status",
            content="paused",
        ),
        today=date(2026, 7, 10),
    )
    text = (tmp_path / "projects" / "foo" / "overview.md").read_text(encoding="utf-8")
    assert "status: paused" in text
    assert "updated: 2026-07-10" in text  # updated проставлен кодом
    assert "created: 2026-01-01" in text  # created сохранён


def test_apply_update_field_missing_file(tmp_path: Path) -> None:
    with pytest.raises(MemoryApplyError, match="не существует"):
        apply_change(
            tmp_path,
            MemoryChangeRequest(
                file="ghost.md",
                operation=MemoryOperation.UPDATE_FIELD,
                field="status",
                content="paused",
            ),
        )


def test_apply_update_field_no_frontmatter(tmp_path: Path) -> None:
    (tmp_path / "plain.md").write_text("просто текст без frontmatter\n", encoding="utf-8")
    with pytest.raises(MemoryApplyError, match="frontmatter"):
        apply_change(
            tmp_path,
            MemoryChangeRequest(
                file="plain.md",
                operation=MemoryOperation.UPDATE_FIELD,
                field="status",
                content="paused",
            ),
        )


def test_apply_delete(tmp_path: Path) -> None:
    (tmp_path / "old.md").write_text("удалить", encoding="utf-8")
    apply_change(tmp_path, MemoryChangeRequest(file="old.md", operation=MemoryOperation.DELETE))
    assert not (tmp_path / "old.md").exists()


def test_apply_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(MemoryApplyError, match="пределы"):
        apply_change(
            tmp_path,
            MemoryChangeRequest(file="../escape.md", operation=MemoryOperation.CREATE, content="x"),
        )


# --- reader ---


def test_read_memory_injects_only_hot_files(tmp_path: Path) -> None:
    # В контекст идут только index.md и user/profile.md; остальное — по требованию.
    (tmp_path / "user").mkdir()
    (tmp_path / "user/profile.md").write_text("любит Python", encoding="utf-8")
    (tmp_path / "index.md").write_text("# Индекс памяти\n## Проекты", encoding="utf-8")
    (tmp_path / "projects" / "svarog").mkdir(parents=True)
    (tmp_path / "projects/svarog/overview.md").write_text("секрет проекта", encoding="utf-8")
    (tmp_path / "decisions").mkdir()
    (tmp_path / "decisions/adr.md").write_text("детали решения", encoding="utf-8")
    text = read_memory(tmp_path)
    assert "любит Python" in text
    assert "Индекс памяти" in text
    assert "секрет проекта" not in text  # страница проекта не инъектится
    assert "детали решения" not in text  # decisions не инъектятся


def test_read_memory_respects_limit(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("x" * 5000, encoding="utf-8")
    (tmp_path / "user").mkdir()
    (tmp_path / "user/profile.md").write_text("y" * 5000, encoding="utf-8")
    text = read_memory(tmp_path, limit_bytes=3000)
    assert "превысил лимит контекста" in text


def test_read_memory_missing_dir(tmp_path: Path) -> None:
    assert read_memory(tmp_path / "nope") == ""


# --- single writer ---


async def _memory_repo(tmp_path: Path) -> Path:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    repo = GitRepo(memory_dir)
    await repo.init()
    await repo.ensure_identity()
    return memory_dir


async def test_writer_applies_and_commits_sequentially(db: AsyncSession, tmp_path: Path) -> None:
    memory_dir = await _memory_repo(tmp_path)
    writer = MemoryWriter(db, memory_dir)
    run1 = await _make_run(db, "первый")
    run2 = await _make_run(db, "второй")

    await writer.enqueue(
        MemoryChangeRequest(
            file="user/profile.md",
            operation=MemoryOperation.CREATE,
            content="# Профиль\nлюбит Python\n",
            source_run_id=run1,
        )
    )
    await writer.enqueue(
        MemoryChangeRequest(
            file="user/profile.md",
            operation=MemoryOperation.APPEND,
            content="и краткость\n",
            source_run_id=run2,
        )
    )
    processed = await writer.drain()
    assert len(processed) == 2
    assert all(row.status is MemoryChangeStatus.APPLIED for row in processed)
    assert all(row.commit_sha for row in processed)

    text = (memory_dir / "user/profile.md").read_text(encoding="utf-8")
    assert "любит Python" in text
    assert "и краткость" in text

    # Каждая заявка — отдельный коммит с trailer Run-Id; сверху reindex-коммит
    # (ADR-0011, автоген index.md/log.md) без Run-Id.
    repo = GitRepo(memory_dir)
    _, head, _ = await repo._git("log", "--format=%s", "-n", "1")
    assert head.strip() == "memory: reindex"
    _, log, _ = await repo._git("log", "--format=%B", "-n", "3")
    assert f"Run-Id: {run2}" in log
    assert f"Run-Id: {run1}" in log


async def test_writer_blocks_commit_on_secret(db: AsyncSession, tmp_path: Path) -> None:
    memory_dir = await _memory_repo(tmp_path)
    writer = MemoryWriter(db, memory_dir)
    run_id = await _make_run(db, "секретный")
    # Заведомо ненастоящий токен (публичный AWS example).
    await writer.enqueue(
        MemoryChangeRequest(
            file="creds.md",
            operation=MemoryOperation.CREATE,
            content="ключ = AKIAIOSFODNN7EXAMPLE\n",
            source_run_id=run_id,
        )
    )
    processed = await writer.drain()
    assert processed[0].status is MemoryChangeStatus.FAILED
    assert processed[0].error is not None
    assert "secret" in processed[0].error.lower()


async def test_writer_second_drain_noop(db: AsyncSession, tmp_path: Path) -> None:
    memory_dir = await _memory_repo(tmp_path)
    writer = MemoryWriter(db, memory_dir)
    await writer.enqueue(
        MemoryChangeRequest(file="a.md", operation=MemoryOperation.CREATE, content="x\n")
    )
    await writer.drain()
    assert await writer.drain() == []
    remaining = (
        (
            await db.execute(
                select(MemoryChange).where(MemoryChange.status == MemoryChangeStatus.PENDING)
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []


# --- RememberTool: валидация заявки в момент вызова ---


def _remember_tool(tmp_path: Path) -> tuple["RememberTool", list[MemoryChangeRequest]]:
    sink: list[MemoryChangeRequest] = []
    return RememberTool(on_enqueue=sink.append, memory_dir=tmp_path), sink


async def test_remember_rejects_create_over_existing_file(tmp_path: Path) -> None:
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "profile.md").write_text("# Профиль\n", encoding="utf-8")
    tool, sink = _remember_tool(tmp_path)
    result = await tool.call({"file": "user/profile.md", "operation": "create", "content": "x"})
    assert not result.ok
    assert result.error is not None and "уже существует" in result.error
    assert sink == []


async def test_remember_rejects_replace_of_missing_section(tmp_path: Path) -> None:
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "profile.md").write_text("# Профиль\n\n## Дейлики\n- 13:00\n", "utf-8")
    tool, sink = _remember_tool(tmp_path)
    result = await tool.call(
        {
            "file": "user/profile.md",
            "operation": "replace_section",
            "section": "Нет",
            "content": "x",
        }
    )
    assert not result.ok
    assert result.error is not None and "не найдена" in result.error
    assert sink == []


async def test_remember_rejects_escaping_path(tmp_path: Path) -> None:
    tool, sink = _remember_tool(tmp_path)
    result = await tool.call({"file": "../outside.md", "operation": "append", "content": "x"})
    assert not result.ok
    assert sink == []


async def test_remember_accepts_valid_operations(tmp_path: Path) -> None:
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "profile.md").write_text("# Профиль\n\n## Дейлики\nстарое\n", "utf-8")
    tool, sink = _remember_tool(tmp_path)
    for args in (
        {"file": "user/profile.md", "operation": "append", "content": "новое\n"},
        {
            "file": "user/profile.md",
            "operation": "replace_section",
            "section": "Дейлики",
            "content": "обновлено\n",
        },
        {"file": "projects/animateyou.md", "operation": "create", "content": "# AnimateYou\n"},
    ):
        result = await tool.call(args)
        assert result.ok, result.error
    assert len(sink) == 3


async def test_remember_allows_chain_create_then_replace(tmp_path: Path) -> None:
    # Очередь применяется после run: replace_section по файлу, который создаст
    # предыдущая заявка этого же run, не должен ложно падать.
    tool, sink = _remember_tool(tmp_path)
    created = await tool.call(
        {"file": "projects/new.md", "operation": "create", "content": "# New\n\n## Статус\nok\n"}
    )
    assert created.ok
    replaced = await tool.call(
        {
            "file": "projects/new.md",
            "operation": "replace_section",
            "section": "Статус",
            "content": "x",
        }
    )
    assert replaced.ok, replaced.error
    assert len(sink) == 2


async def test_remember_update_field_accepted(tmp_path: Path) -> None:
    (tmp_path / "projects" / "foo").mkdir(parents=True)
    (tmp_path / "projects" / "foo" / "overview.md").write_text(
        "---\nname: Foo\nslug: foo\nsummary: s\nstatus: active\n---\n\n## Описание\nт\n",
        encoding="utf-8",
    )
    tool, sink = _remember_tool(tmp_path)
    result = await tool.call(
        {
            "file": "projects/foo/overview.md",
            "operation": "update_field",
            "field": "status",
            "content": "paused",
        }
    )
    assert result.ok, result.error
    assert len(sink) == 1 and sink[0].field == "status"


async def test_remember_update_field_requires_field(tmp_path: Path) -> None:
    (tmp_path / "p.md").write_text("---\nstatus: active\n---\n", encoding="utf-8")
    tool, sink = _remember_tool(tmp_path)
    result = await tool.call({"file": "p.md", "operation": "update_field", "content": "paused"})
    assert not result.ok
    assert result.error is not None and "field" in result.error
    assert sink == []


async def test_remember_update_field_rejects_missing_file(tmp_path: Path) -> None:
    tool, sink = _remember_tool(tmp_path)
    result = await tool.call(
        {"file": "ghost.md", "operation": "update_field", "field": "status", "content": "paused"}
    )
    assert not result.ok
    assert result.error is not None and "не существует" in result.error
    assert sink == []


async def test_remember_update_field_rejects_sources(tmp_path: Path) -> None:
    (tmp_path / "sources" / "x").mkdir(parents=True)
    (tmp_path / "sources" / "x" / "spec.md").write_text("---\nv: 1\n---\n", encoding="utf-8")
    tool, sink = _remember_tool(tmp_path)
    result = await tool.call(
        {
            "file": "sources/x/spec.md",
            "operation": "update_field",
            "field": "v",
            "content": "2",
        }
    )
    assert not result.ok
    assert result.error is not None and "неизменяемый" in result.error
    assert sink == []


# --- read_memory: профиль первым, при усечении режется хвост (index) ---


def test_read_memory_profile_before_index(tmp_path: Path) -> None:
    (tmp_path / "user").mkdir()
    (tmp_path / "index.md").write_text("индекс", encoding="utf-8")
    (tmp_path / "user" / "profile.md").write_text("профиль", encoding="utf-8")
    text = read_memory(tmp_path)
    assert text.index("user/profile.md") < text.index("index.md")


def test_read_memory_truncation_keeps_profile(tmp_path: Path) -> None:
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "profile.md").write_text("важный профиль", encoding="utf-8")
    (tmp_path / "index.md").write_text("x" * 5000, encoding="utf-8")
    text = read_memory(tmp_path, limit_bytes=200)
    assert "важный профиль" in text
    assert "превысил лимит контекста" in text


# --- ReadMemoryTool: прогрессивная загрузка (ADR-0011) ---

_PROJECT_PAGE = (
    "---\nname: AnimateYou\nslug: animateyou\nsummary: бот\nstatus: active\n---\nОписание.\n"
)


async def test_read_memory_tool_reads_page(tmp_path: Path) -> None:
    (tmp_path / "projects" / "animateyou").mkdir(parents=True)
    (tmp_path / "projects/animateyou/overview.md").write_text(_PROJECT_PAGE, encoding="utf-8")
    result = await ReadMemoryTool(tmp_path).call({"path": "projects/animateyou/overview.md"})
    assert result.ok
    assert "AnimateYou" in result.output


async def test_read_memory_tool_missing_file(tmp_path: Path) -> None:
    result = await ReadMemoryTool(tmp_path).call({"path": "projects/nope/overview.md"})
    assert not result.ok
    assert "не найден" in result.error


async def test_read_memory_tool_rejects_escaping_path(tmp_path: Path) -> None:
    result = await ReadMemoryTool(tmp_path).call({"path": "../../etc/passwd"})
    assert not result.ok


# --- remember: контракт страницы проекта (ADR-0011) ---


async def test_remember_rejects_project_page_without_frontmatter(tmp_path: Path) -> None:
    tool, sink = _remember_tool(tmp_path)
    result = await tool.call(
        {"file": "projects/x/overview.md", "operation": "create", "content": "# без frontmatter\n"}
    )
    assert not result.ok
    assert "frontmatter" in result.error
    assert sink == []


async def test_remember_rejects_project_page_slug_mismatch(tmp_path: Path) -> None:
    tool, _ = _remember_tool(tmp_path)
    result = await tool.call(
        {"file": "projects/other/overview.md", "operation": "create", "content": _PROJECT_PAGE}
    )
    assert not result.ok
    assert "slug" in result.error


async def test_remember_accepts_valid_project_page(tmp_path: Path) -> None:
    tool, sink = _remember_tool(tmp_path)
    result = await tool.call(
        {"file": "projects/animateyou/overview.md", "operation": "create", "content": _PROJECT_PAGE}
    )
    assert result.ok, result.error
    assert len(sink) == 1


# --- remember: raw-слой sources/ неизменяем (ADR-0011) ---


async def test_remember_creates_source(tmp_path: Path) -> None:
    tool, sink = _remember_tool(tmp_path)
    result = await tool.call(
        {"file": "sources/animateyou/spec.md", "operation": "create", "content": "raw spec\n"}
    )
    assert result.ok, result.error
    assert len(sink) == 1


async def test_remember_rejects_append_to_source(tmp_path: Path) -> None:
    (tmp_path / "sources" / "animateyou").mkdir(parents=True)
    (tmp_path / "sources/animateyou/spec.md").write_text("raw\n", encoding="utf-8")
    tool, sink = _remember_tool(tmp_path)
    result = await tool.call(
        {"file": "sources/animateyou/spec.md", "operation": "append", "content": "правка\n"}
    )
    assert not result.ok
    assert "неизменяем" in result.error
    assert sink == []


def test_read_memory_truncates_at_line_boundary_with_recipe(tmp_path: Path) -> None:
    """ADR-0015 §1.5: усечение по границе строки + warning с действием."""
    (tmp_path / "user").mkdir()
    (tmp_path / "user/profile.md").write_text("краткий профиль", encoding="utf-8")
    index_lines = "\n".join(
        f"- [proj{i}](projects/proj{i}/overview.md) — описание" for i in range(200)
    )
    (tmp_path / "index.md").write_text(f"# Индекс памяти\n{index_lines}", encoding="utf-8")

    text = read_memory(tmp_path, limit_bytes=2000)
    # Профиль (первый по порядку) цел, индекс усечён частично, а не заглушкой.
    assert "краткий профиль" in text
    assert "proj0" in text
    assert "proj199" not in text
    # Усечение по границе строки: нет разорванных пополам строк-ссылок.
    for line in text.splitlines():
        if line.startswith("- [proj"):
            assert line.endswith("описание")
    # Warning объясняет, что случилось и что делать.
    assert "WARNING" in text
    assert "превысил лимит" in text
    assert "Сокращай summary" in text
