"""Словарь подсказок для жёстких границ (блок E §1)."""

from svarog_harness.tools.guidance import BoundaryKind, note_for


def test_every_kind_has_a_note() -> None:
    """У каждого класса границы есть непустая подсказка."""
    for kind in BoundaryKind:
        note = note_for(kind)
        assert note.strip()


def test_notes_are_distinct() -> None:
    """Подсказки различаются: одинаковый текст на разные границы бесполезен."""
    notes = {note_for(kind) for kind in BoundaryKind}
    assert len(notes) == len(list(BoundaryKind))


def test_workspace_escape_note_names_futile_workarounds() -> None:
    """Подсказка про workspace перечисляет бесполезные обходы и даёт выход."""
    note = note_for(BoundaryKind.WORKSPACE_ESCAPE)
    assert "симлинк" in note
    assert "ask_user" in note


def test_control_dir_note_names_reserved_dirs() -> None:
    note = note_for(BoundaryKind.CONTROL_DIR_WRITE)
    assert ".git" in note
    assert ".svarog" in note


def test_policy_deny_note_points_to_approval() -> None:
    """Отказ политики: повтор бесполезен, но есть легальный путь."""
    note = note_for(BoundaryKind.POLICY_DENY)
    assert "request_approval" in note


def test_approval_denied_note_discourages_identical_retry() -> None:
    note = note_for(BoundaryKind.APPROVAL_DENIED)
    assert "повтор" in note.lower()
