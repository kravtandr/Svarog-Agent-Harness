"""Вложения к сообщению: приём файла и его сокрытие от git."""

from pathlib import Path

from svarog_harness.gitflow import GitRepo

# Каталог вложений внутри workspace. Точка в начале — чтобы
# `list_workspace_files` его пропускал и вложения не засоряли подсказки `@`.
ATTACHMENTS_DIR = ".attachments"


async def ensure_git_excluded(workspace: Path, pattern: str) -> None:
    """Дописать pattern в `.git/info/exclude` рабочего дерева, идемпотентно.

    Именно `info/exclude`, а не `.gitignore`: этот файл локальный и не
    отслеживается, поэтому служебное исключение не попадёт в чужой diff.
    Без него автокоммит (Flow C) утащил бы вложения в историю task-ветки.
    """
    git_dir = await GitRepo(workspace).git_dir()
    if git_dir is None:
        return  # рабочая папка без git — прятать не от кого
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if pattern in existing.splitlines():
        return
    prefix = "" if existing.endswith("\n") or not existing else "\n"
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(f"{prefix}{pattern}\n")
