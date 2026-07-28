"""Вложения к сообщению: запись, лимиты, исключение из git (план 2026-07-28)."""

from pathlib import Path

import pytest

from svarog_harness.gateway.attachments import ensure_git_excluded
from svarog_harness.gitflow import GitRepo


@pytest.mark.asyncio
async def test_exclude_lands_in_separate_git_dir(tmp_path: Path) -> None:
    """При separate_git_dir исключение пишется в настоящий git-каталог,
    а не в workspace/.git (там просто файл-указатель)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    gitdir = tmp_path / "gitdir"
    repo = GitRepo(ws)
    await repo.init(separate_git_dir=gitdir)

    await ensure_git_excluded(ws, ".attachments/")

    exclude = gitdir / "info" / "exclude"
    assert exclude.is_file()
    assert ".attachments/" in exclude.read_text(encoding="utf-8").splitlines()


@pytest.mark.asyncio
async def test_exclude_is_written_once(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    repo = GitRepo(ws)
    await repo.init()

    await ensure_git_excluded(ws, ".attachments/")
    await ensure_git_excluded(ws, ".attachments/")

    exclude = ws / ".git" / "info" / "exclude"
    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert lines.count(".attachments/") == 1, "повторный вызов не дублирует строку"


@pytest.mark.asyncio
async def test_exclude_is_noop_without_git(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    await ensure_git_excluded(ws, ".attachments/")  # не падает


@pytest.mark.asyncio
async def test_exclude_not_fooled_by_substring_match(tmp_path: Path) -> None:
    """`.attachments/` — подстрока `.attachments-old/`, но не та же строка;
    должна быть дописана отдельной строкой, а не принята за уже имеющуюся."""
    ws = tmp_path / "ws"
    ws.mkdir()
    repo = GitRepo(ws)
    await repo.init()
    exclude = ws / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(".attachments-old/\n", encoding="utf-8")

    await ensure_git_excluded(ws, ".attachments/")

    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert ".attachments-old/" in lines
    assert ".attachments/" in lines
