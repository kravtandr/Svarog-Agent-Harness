"""Тесты правил допустимости memory proposal (блок C §3)."""

from pathlib import Path

from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.proposal import MemoryProposalRequest, validate_proposal


def _proposal(
    *changes: MemoryChangeRequest, title: str = "правка", rationale: str = "зачем"
) -> MemoryProposalRequest:
    return MemoryProposalRequest(title=title, rationale=rationale, changes=tuple(changes))


def _append(file: str = "notes.md") -> MemoryChangeRequest:
    return MemoryChangeRequest(file=file, operation=MemoryOperation.APPEND, content="текст")


def test_valid_proposal_has_no_errors(tmp_path: Path) -> None:
    assert validate_proposal(tmp_path, _proposal(_append())) == []


def test_empty_rationale_rejected(tmp_path: Path) -> None:
    """Человек, читающий proposal через месяц, должен видеть зачем правка."""
    errors = validate_proposal(tmp_path, _proposal(_append(), rationale="   "))
    assert any("rationale" in e for e in errors)


def test_empty_title_rejected(tmp_path: Path) -> None:
    errors = validate_proposal(tmp_path, _proposal(_append(), title=""))
    assert any("title" in e for e in errors)


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
            MemoryChangeRequest(file="projects/a/overview.md", operation=MemoryOperation.DELETE)
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


def test_delete_outside_memory_rejected(tmp_path: Path) -> None:
    """Правило про пустые файлы не должно открывать выход за пределы памяти."""
    errors = validate_proposal(
        tmp_path,
        _proposal(MemoryChangeRequest(file="../victim.txt", operation=MemoryOperation.DELETE)),
    )
    assert any("выходит за пределы" in e for e in errors)


def test_change_level_error_is_reported_with_index(tmp_path: Path) -> None:
    """Ошибка отдельной правки называет её номер — иначе непонятно, какая из пачки."""
    (tmp_path / "notes.md").write_text("есть\n", encoding="utf-8")
    errors = validate_proposal(
        tmp_path,
        _proposal(
            _append("other.md"),
            MemoryChangeRequest(
                file="notes.md", operation=MemoryOperation.CREATE, content="перезапись"
            ),
        ),
    )
    assert any(e.startswith("правка 2:") for e in errors)
