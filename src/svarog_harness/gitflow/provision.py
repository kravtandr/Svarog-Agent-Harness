"""Провижн серверных workspaces для cloud-режима (ADR-0017 §1).

Два источника workspace для run'а gateway: одноразовый task-workspace,
склонированный host-side из git-репо клиента, и постоянный named workspace
тенанта. Оба живут строго под workspace-root тенанта (confinement, как в
ADR-0014); credentials клона резолвятся из tenant SecretStore только на
хосте и не попадают в sandbox/контекст/trace.
"""

import asyncio
import os
import re
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from svarog_harness.gitflow.repo import _HARDENED_GIT_ENV, _HARDENED_GIT_FLAGS

# Имя named workspace — слаг, не путь: `..`/`/`/юникод отклоняются до
# любого резолвинга (ADR-0017, инвариант named-workspace confinement).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Допустимые схемы клона: https и ssh (обе формы). `file://`, локальные пути,
# `ext::` и прочие транспорты отклоняются до запуска git; вторым эшелоном
# `protocol.file.allow=never`/`protocol.ext.allow=never` на самом клоне.
_HTTPS_RE = re.compile(r"^https://[^/\s]+/\S+$")
_SSH_URL_RE = re.compile(r"^ssh://[^/\s]+/\S+$")
_SCP_LIKE_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:(?!//)\S+$")

# Конвенциональное имя секрета с git-credentials тенанта (ADR-0017 развилка 3).
DEFAULT_GIT_CREDENTIALS_REF = "git.credentials"

# Второй эшелон после validate_repo_url: file-транспорт закрыт и на уровне git
# (кросс-тенантное чтение хоста через clone). Module-константа — тестовый шов:
# интеграционные тесты клонируют локальный путь, ослабляя её monkeypatch'ем.
_PROTOCOL_FLAGS = ("-c", "protocol.file.allow=never")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class WorkspaceNameError(Exception):
    """Имя named workspace не является допустимым слагом."""


class UnknownWorkspaceError(Exception):
    """Named workspace с таким именем не создан (run по опечатке — 404, не mkdir)."""


class WorkspaceExistsError(Exception):
    """Named workspace с таким именем уже существует."""


class WorkspaceLimitError(Exception):
    """Достигнут потолок named workspaces тенанта (cloud.max_named_workspaces)."""


class RepoUrlError(Exception):
    """URL репозитория отклонён (схема вне allowlist https/ssh)."""


class CloneError(Exception):
    """git clone завершился с ошибкой (stderr — без значений секретов)."""


def named_root(workspace_root: Path) -> Path:
    """Корень named workspaces тенанта: `<workspace_root>/named`."""
    return workspace_root / "named"


def tasks_root(workspace_root: Path) -> Path:
    """Корень одноразовых task-workspaces: `<workspace_root>/tasks`."""
    return workspace_root / "tasks"


def validate_workspace_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise WorkspaceNameError(
            f"недопустимое имя workspace {name!r}: слаг [a-z0-9-], до 64 символов"
        )
    return name


def named_workspace_path(workspace_root: Path, name: str) -> Path:
    """Путь named workspace; имя валидируется, существование не проверяется."""
    return named_root(workspace_root) / validate_workspace_name(name)


def resolve_named_workspace(workspace_root: Path, name: str) -> Path:
    """Путь существующего named workspace; неизвестное имя — ошибка (не mkdir).

    Run по несуществующему имени отвечает 404, а не создаёт каталог молча —
    защита от опечаток, размазывающих результаты (ADR-0017 §1).
    """
    path = named_workspace_path(workspace_root, name)
    if not path.is_dir():
        raise UnknownWorkspaceError(f"named workspace '{name}' не создан (POST /workspaces)")
    return path


def create_named_workspace(workspace_root: Path, name: str, *, limit: int) -> Path:
    path = named_workspace_path(workspace_root, name)
    if path.exists():
        raise WorkspaceExistsError(f"named workspace '{name}' уже существует")
    if limit <= 0:
        raise WorkspaceLimitError("named workspaces выключены (cloud.max_named_workspaces=0)")
    existing = len(list_named_workspaces(workspace_root))
    if existing >= limit:
        raise WorkspaceLimitError(
            f"потолок named workspaces достигнут ({existing}/{limit}, cloud.max_named_workspaces)"
        )
    path.mkdir(parents=True)
    return path


@dataclass(frozen=True)
class NamedWorkspaceInfo:
    name: str
    path: Path
    size_bytes: int
    modified_at: datetime


def list_named_workspaces(workspace_root: Path) -> list[NamedWorkspaceInfo]:
    root = named_root(workspace_root)
    if not root.is_dir():
        return []
    infos: list[NamedWorkspaceInfo] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.is_symlink() or not _NAME_RE.match(entry.name):
            continue  # чужеродные записи (symlink/файл/не-слаг) не считаем workspace'ами
        infos.append(
            NamedWorkspaceInfo(
                name=entry.name,
                path=entry,
                size_bytes=_tree_size(entry),
                modified_at=datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC),
            )
        )
    return infos


def delete_named_workspace(workspace_root: Path, name: str) -> None:
    path = resolve_named_workspace(workspace_root, name)
    shutil.rmtree(path)


def resolve_workspace_file(workspace_root: Path, name: str, relative: str) -> Path:
    """Путь внутри named workspace с confinement: выход за его границы — ошибка.

    Symlink-escape закрывается resolve(): итоговый реальный путь обязан
    остаться под каталогом workspace (тот же принцип, что у file_tools).
    """
    base = resolve_named_workspace(workspace_root, name).resolve()
    target = (base / relative.lstrip("/")).resolve()
    if target != base and base not in target.parents:
        raise WorkspaceNameError(f"путь {relative!r} выходит за пределы workspace '{name}'")
    return target


def _tree_size(root: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            with_stat = Path(dirpath) / fname
            try:
                st = with_stat.lstat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


# --- task-workspace из git-клона -----------------------------------------


def task_workspace_dir(workspace_root: Path, task: str) -> Path:
    """Уникальный каталог одноразового task-workspace: `tasks/<слаг>-<id>`."""
    slug = _SLUG_RE.sub("-", task.lower()).strip("-")[:32].strip("-") or "task"
    return tasks_root(workspace_root) / f"{slug}-{uuid.uuid4().hex[:8]}"


def validate_repo_url(url: str) -> str:
    """Разрешить только https/ssh; `file://`, локальные пути, `ext::` — отказ."""
    if _HTTPS_RE.match(url) or _SSH_URL_RE.match(url) or _SCP_LIKE_RE.match(url):
        return url
    raise RepoUrlError(f"URL репозитория отклонён (допустимы https:// и ssh): {url!r}")


async def provision_clone(
    url: str,
    dest: Path,
    *,
    ref: str | None = None,
    credentials: str | None = None,
) -> Path:
    """Host-side clone репозитория клиента в task-workspace (ADR-0017 §1).

    Credentials (значение секрета из tenant-store) передаются git'у только
    через одноразовый GIT_ASKPASS-скрипт (https) — в env агента, sandbox и
    trace они не попадают. Формат значения: `user:token` либо просто token
    (username тогда `x-access-token` — работает для PAT GitHub/GitLab).
    """
    validate_repo_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise CloneError(f"целевой каталог уже существует: {dest}")

    env = dict(os.environ)
    env.update(_HARDENED_GIT_ENV)
    env["GIT_TERMINAL_PROMPT"] = "0"  # никаких интерактивных запросов на сервере

    askpass_dir: str | None = None
    if credentials is not None and url.startswith("https://"):
        askpass_dir, askpass = _write_askpass(credentials)
        env["GIT_ASKPASS"] = str(askpass)

    args = ["clone", *(("--branch", ref) if ref else ()), "--", url, str(dest)]
    try:
        code, _out, err = await _run_git(args, env=env)
        if code != 0:
            detail = _strip_secrets(err, credentials)
            raise CloneError(f"git clone не удался (код {code}): {detail}")
    finally:
        if askpass_dir is not None:
            shutil.rmtree(askpass_dir, ignore_errors=True)
    return dest


async def _run_git(args: list[str], *, env: dict[str, str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *_HARDENED_GIT_FLAGS,
        *_PROTOCOL_FLAGS,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_bytes.decode(errors="replace"),
        stderr_bytes.decode(errors="replace"),
    )


def _write_askpass(credentials: str) -> tuple[str, Path]:
    """Одноразовый GIT_ASKPASS: отвечает git'у username/password, файл 0700."""
    user, _, password = credentials.partition(":")
    if not password:
        user, password = "x-access-token", credentials
    tmp = tempfile.mkdtemp(prefix="svarog-askpass-")
    script = Path(tmp) / "askpass.sh"
    # Значения в single quotes с экранированием: не исполняются shell'ом.
    q_user, q_pass = _sh_quote(user), _sh_quote(password)
    script.write_text(
        "#!/bin/sh\n"
        f'case "$1" in\n'
        f"  Username*) printf '%s' {q_user} ;;\n"
        f"  *) printf '%s' {q_pass} ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return tmp, script


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _strip_secrets(text: str, credentials: str | None) -> str:
    """Убрать значение секрета из сообщения об ошибке (git печатает URL)."""
    if not credentials:
        return text.strip()
    cleaned = text.replace(credentials, "***")
    _, _, password = credentials.partition(":")
    if password:
        cleaned = cleaned.replace(password, "***")
    return cleaned.strip()


# --- retention-GC task-workspaces (named не трогаем никогда) ---------------


def stale_task_workspaces(
    workspace_root: Path, *, retention_days: int, active: set[str]
) -> list[Path]:
    """Терминальные task-workspaces старше retention (кандидаты на удаление).

    `active` — str-пути workspace'ов незавершённых runs (PENDING/RUNNING/
    SUSPENDED/WAITING_APPROVAL): такие не трогаем, resume должен работать.
    Named workspaces GC не подлежат по построению (ADR-0017).
    """
    if retention_days <= 0:
        return []
    root = tasks_root(workspace_root)
    if not root.is_dir():
        return []
    cutoff = datetime.now(tz=UTC) - timedelta(days=retention_days)
    stale: list[Path] = []
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        if str(entry) in active:
            continue
        modified = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)
        if modified < cutoff:
            stale.append(entry)
    return stale


def sweep_task_workspaces(
    workspace_root: Path, *, retention_days: int, active: set[str]
) -> list[Path]:
    """Удалить протухшие task-workspaces; вернуть удалённые пути."""
    removed = []
    for path in stale_task_workspaces(workspace_root, retention_days=retention_days, active=active):
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path)
    return removed


__all__ = [
    "DEFAULT_GIT_CREDENTIALS_REF",
    "CloneError",
    "NamedWorkspaceInfo",
    "RepoUrlError",
    "UnknownWorkspaceError",
    "WorkspaceExistsError",
    "WorkspaceLimitError",
    "WorkspaceNameError",
    "create_named_workspace",
    "delete_named_workspace",
    "list_named_workspaces",
    "named_workspace_path",
    "provision_clone",
    "resolve_named_workspace",
    "resolve_workspace_file",
    "sweep_task_workspaces",
    "task_workspace_dir",
    "validate_repo_url",
]
