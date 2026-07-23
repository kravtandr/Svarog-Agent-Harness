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
