"""Вложения к сообщению: приём файла и его сокрытие от git."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from svarog_harness.gitflow import GitRepo
from svarog_harness.tools.document_tools import (
    _DOCUMENT_SUFFIXES,
    _IMAGE_LIMIT_BYTES,
    _IMAGE_MIME,
)

# Каталог вложений внутри workspace. Точка в начале — чтобы
# `list_workspace_files` его пропускал и вложения не засоряли подсказки `@`.
ATTACHMENTS_DIR = ".attachments"

# Белый список строится из того, что инструменты действительно умеют, а не
# копируется: иначе разойдётся с ними при первом расширении набора.
ALLOWED_SUFFIXES: frozenset[str] = frozenset(
    set(_IMAGE_MIME) | set(_DOCUMENT_SUFFIXES) | {".txt", ".md", ".json", ".yaml", ".yml"}
)
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class AttachmentTypeError(ValueError):
    """Расширение вне белого списка; наружу — HTTP 415."""


class AttachmentTooLarge(ValueError):  # noqa: N818 — имя из интерфейса задачи 7
    """Файл больше потолка; наружу — HTTP 413."""


@dataclass(frozen=True)
class StoredAttachment:
    path: str  # относительно workspace: ".attachments/ab12_скрин.png"
    name: str  # исходное имя, как его видит человек
    size_bytes: int
    mime: str | None
    too_large_for_vision: bool


def safe_name(raw: str) -> str:
    """Только базовое имя: разделители путей и `..` отбрасываются."""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = name.lstrip(".") if name.startswith("..") else name
    return name or "файл"


async def store_attachment(workspace: Path, name: str, data: bytes) -> StoredAttachment:
    """Записать вложение в `.attachments/` и спрятать каталог от git."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise AttachmentTooLarge(f"файл {len(data)} байт больше потолка {MAX_UPLOAD_BYTES}")
    clean = safe_name(name)
    suffix = Path(clean).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        allowed = ", ".join(sorted(ALLOWED_SUFFIXES))
        raise AttachmentTypeError(f"расширение '{suffix}' не поддержано; доступны: {allowed}")

    root = (workspace / ATTACHMENTS_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Префикс-хеш от содержимого и имени: коллизий нет, отказывать при
    # повторной загрузке не нужно, а человеку показывается исходное имя.
    digest = hashlib.sha256(data + clean.encode("utf-8")).hexdigest()[:8]
    target = (root / f"{digest}_{clean}").resolve()
    if not target.is_relative_to(root):
        raise AttachmentTypeError(f"имя '{name}' выводит за пределы {ATTACHMENTS_DIR}")
    target.write_bytes(data)
    await ensure_git_excluded(workspace, f"{ATTACHMENTS_DIR}/")

    mime = _IMAGE_MIME.get(suffix)
    return StoredAttachment(
        path=f"{ATTACHMENTS_DIR}/{target.name}",
        name=clean,
        size_bytes=len(data),
        mime=mime,
        too_large_for_vision=mime is not None and len(data) > _IMAGE_LIMIT_BYTES,
    )


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
