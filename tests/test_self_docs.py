"""Self-документация Svarog в sandbox внешнего агента.

Спека: docs/superpowers/specs/2026-07-22-self-docs-design.md
"""

from pathlib import Path

import pytest

from svarog_harness.runtime import self_docs
from svarog_harness.runtime.self_docs import (
    resolve_docs_root,
    self_docs_hint,
    stage_self_docs,
)


def _fake_root(tmp_path: Path, *, with_agents: bool = True) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "README.md").write_text("# Svarog\n\nкоманды\n", encoding="utf-8")
    if with_agents:
        (root / "AGENTS.md").write_text("# правила\n", encoding="utf-8")
    (root / "docs" / "adr" / "0001-first.md").write_text(
        "\n# ADR-0001. Первое решение\n\ntext\n", encoding="utf-8"
    )
    (root / "docs" / "adr" / "0016-exec.md").write_text(
        "# ADR-0016. External Agent Executor\n", encoding="utf-8"
    )
    return root


def test_resolve_docs_root_finds_repo() -> None:
    root = resolve_docs_root()
    assert root is not None
    assert (root / "README.md").is_file()
    assert (root / "docs" / "adr").is_dir()


def test_stage_copies_docs_and_builds_index(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    dest = tmp_path / "staged"
    result = stage_self_docs(dest, root=root)
    assert result == dest
    assert (dest / "README.md").is_file()
    assert (dest / "AGENTS.md").is_file()
    assert (dest / "adr" / "0001-first.md").is_file()
    index = (dest / "INDEX.md").read_text(encoding="utf-8")
    assert "README.md" in index
    assert "AGENTS.md" in index
    # Заголовок ADR берётся из первой заголовочной строки.
    assert "ADR-0016. External Agent Executor" in index
    assert "adr/0016-exec.md" in index


def test_stage_without_agents_omits_it(tmp_path: Path) -> None:
    root = _fake_root(tmp_path, with_agents=False)
    dest = tmp_path / "staged"
    stage_self_docs(dest, root=root)
    assert not (dest / "AGENTS.md").exists()
    assert "AGENTS.md" not in (dest / "INDEX.md").read_text(encoding="utf-8")


def test_stage_returns_none_when_no_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(self_docs, "resolve_docs_root", lambda: None)
    assert stage_self_docs(tmp_path / "staged") is None


def test_hint_mentions_index_and_path() -> None:
    hint = self_docs_hint("/opt/svarog-docs")
    assert "/opt/svarog-docs/INDEX.md" in hint
    assert "Svarog" in hint
