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


class AttachmentPathError(ValueError):
    """Путь вложения не из `.attachments/` этой сессии; наружу — HTTP 400."""


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

    ws = workspace.resolve()
    root = ws / ATTACHMENTS_DIR
    if root.is_symlink():
        # `.attachments` сам — симлинк наружу: resolve() ниже увёл бы root на
        # цель симлинка, и любой последующий is_relative_to(root) проходил бы
        # для чего угодно внутри неё (находка 2 финального ревью). Симлинк
        # *внутри* .attachments уже ловится ниже (target.is_relative_to(root));
        # здесь — сама точка входа.
        raise AttachmentTypeError(f"{ATTACHMENTS_DIR} — симлинк: отказано")
    root = root.resolve()
    if not root.is_relative_to(ws):
        raise AttachmentTypeError(f"{ATTACHMENTS_DIR} ведёт за пределы рабочей папки")
    root.mkdir(parents=True, exist_ok=True)
    # Префикс-хеш от содержимого и имени: коллизий нет, отказывать при
    # повторной загрузке не нужно, а человеку показывается исходное имя.
    digest = hashlib.sha256(data + clean.encode("utf-8")).hexdigest()[:8]
    try:
        target = (root / f"{digest}_{clean}").resolve()
    except (OSError, ValueError) as exc:
        # NUL-байт в имени валит `resolve()` в ValueError ещё до всякой
        # записи — превращаем в тот же 415, что и неподдержанное расширение,
        # а не даём сырому ValueError утечь наружу как 500.
        raise AttachmentTypeError(f"имя '{name}' нельзя использовать как файл: {exc}") from exc
    if not target.is_relative_to(root):
        raise AttachmentTypeError(f"имя '{name}' выводит за пределы {ATTACHMENTS_DIR}")
    try:
        target.write_bytes(data)
    except (OSError, ValueError) as exc:
        # Тот же случай, но для имён, которые resolve() пропустил, а упали
        # уже на самой записи (например, слишком длинное имя — ENAMETOOLONG).
        raise AttachmentTypeError(f"имя '{name}' нельзя использовать как файл: {exc}") from exc
    await ensure_git_excluded(workspace, f"{ATTACHMENTS_DIR}/")

    mime = _IMAGE_MIME.get(suffix)
    return StoredAttachment(
        path=f"{ATTACHMENTS_DIR}/{target.name}",
        name=clean,
        size_bytes=len(data),
        mime=mime,
        too_large_for_vision=mime is not None and len(data) > _IMAGE_LIMIT_BYTES,
    )


def attachments_note(paths: list[str]) -> str:
    """Строка, которой сообщение сообщает агенту о вложениях.

    Дописывается к тексту задачи и попадает в трассу — то есть в ленте
    видно ровно то, что получил агент, без скрытых добавок.
    """
    listed = ", ".join(paths)
    return f"Вложения (прочитай их read_image / read_document): {listed}"


def verify_attachment(workspace: Path, rel: str) -> Path:
    """Путь обязан лежать в `.attachments/` этой рабочей папки и существовать."""
    ws = workspace.resolve()
    root = ws / ATTACHMENTS_DIR
    if root.is_symlink():
        # См. store_attachment: `.attachments` сам — симлинк наружу обходит
        # проверку ниже, потому что resolve() увёл бы root на цель симлинка, и
        # is_relative_to(root) стал бы тривиально верным для чего угодно там.
        raise AttachmentPathError(f"{ATTACHMENTS_DIR} — симлинк: отказано")
    root = root.resolve()
    if not root.is_relative_to(ws):
        raise AttachmentPathError(f"{ATTACHMENTS_DIR} ведёт за пределы рабочей папки")
    candidate = (workspace / rel).resolve()
    if not candidate.is_relative_to(root):
        raise AttachmentPathError(f"вложение '{rel}' не из {ATTACHMENTS_DIR} этой сессии")
    if not candidate.is_file():
        raise AttachmentPathError(f"вложения '{rel}' нет на диске")
    return candidate


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
