"""Вложения к сообщению: приём файла и его сокрытие от git."""

from pathlib import Path

from svarog_harness.gitflow import GitRepo

# Каталог вложений внутри workspace. Точка в начале — чтобы
# `list_workspace_files` его пропускал и вложения не засоряли подсказки `@`.
ATTACHMENTS_DIR = ".attachments"


async def ensure_git_excluded(workspace: Path, pattern: str) -> None:
    """Спрятать pattern от git через `info/exclude` рабочего дерева.

    Вся работа — в `GitRepo.ensure_excluded`: он идемпотентен и находит файл
    через `rev-parse --git-path`, что верно и при separate_git_dir. Здесь
    добавляется только терпимость к папке без git: прятать не от кого.
    """
    repo = GitRepo(workspace)
    if not await repo.is_repo():
        return  # рабочая папка без git — прятать не от кого
    await repo.ensure_excluded(pattern)
