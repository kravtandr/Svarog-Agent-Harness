"""Вложения к сообщению: запись, лимиты, исключение из git (план 2026-07-28)."""

from pathlib import Path

import pytest

from svarog_harness.gateway.attachments import ensure_git_excluded
from svarog_harness.gitflow import GitRepo


@pytest.mark.asyncio
async def test_git_dir_resolves_separate_git_dir(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    gitdir = tmp_path / "gitdir"
    repo = GitRepo(ws)
    await repo.init(separate_git_dir=gitdir)

    found = await repo.git_dir()

    assert found is not None
    assert found.resolve() == gitdir.resolve(), "не workspace/.git, а настоящий каталог"


@pytest.mark.asyncio
async def test_exclude_is_written_once(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    repo = GitRepo(ws)
    await repo.init()

    await ensure_git_excluded(ws, ".attachments/")
    await ensure_git_excluded(ws, ".attachments/")

    exclude = (await repo.git_dir()) / "info" / "exclude"
    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert lines.count(".attachments/") == 1, "повторный вызов не дублирует строку"


@pytest.mark.asyncio
async def test_exclude_is_noop_without_git(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    await ensure_git_excluded(ws, ".attachments/")  # не падает
