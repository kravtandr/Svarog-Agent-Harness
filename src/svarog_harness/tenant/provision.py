"""Провижн тенанта (ADR-0012/0014): дерево home + git init + БД + bearer-token.

Переиспользует ту же init-логику, что `svarog init` (GitRepo, init_db). Реестр
резервирует запись первым (падает при дубле); при последующей ошибке — best-
effort откат записи и свежесозданного home.
"""

from __future__ import annotations

import contextlib
import secrets as _secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from svarog_harness.config.paths import tenant_home
from svarog_harness.config.schema import SvarogConfig, TenantRole
from svarog_harness.gitflow import GitError, GitRepo
from svarog_harness.secrets.store import FileSecretStore
from svarog_harness.storage.db import init_db
from svarog_harness.tenant.registry import TenantRegistry

# Ref, под которым в secrets.json тенанта хранится его текущий gateway-token.
GATEWAY_TOKEN_REF = "gateway-token"
_SUBDIRS = ("memory", "skills", "workspaces", "policies")


@dataclass(frozen=True)
class ProvisionResult:
    tenant_id: str
    role: TenantRole
    home: Path
    token: str


async def _git_init(path: Path, message: str) -> None:
    repo = GitRepo(path)
    if not await repo.is_repo():
        await repo.init()
        await repo.ensure_identity()
        await repo.add_all()
        with contextlib.suppress(GitError):
            await repo.commit(message)


def _issue_token() -> str:
    return _secrets.token_urlsafe(32)


def _gateway_principal(token: str) -> str:
    return f"gateway:{token}"


async def provision_tenant(
    cfg: SvarogConfig,
    registry: TenantRegistry,
    tenant_id: str,
    role: TenantRole,
) -> ProvisionResult:
    """Завести тенанта: home-дерево, git-репозитории памяти/скиллов, БД, токен."""
    home = tenant_home(cfg, tenant_id)
    home_existed = home.exists()
    registry.create(tenant_id, role)  # TenantExistsError при дубле — до создания файлов
    try:
        for sub in _SUBDIRS:
            (home / sub).mkdir(parents=True, exist_ok=True)
        await _git_init(home / "memory", "svarog tenant: memory repo")
        await _git_init(home / "skills", "svarog tenant: skills repo")
        init_db(home / "svarog.db")
        token = _issue_token()
        FileSecretStore(home / "secrets.json").set(GATEWAY_TOKEN_REF, token)
        registry.add_principal(tenant_id, _gateway_principal(token))
    except Exception:
        registry.delete(tenant_id)  # откат control-plane записи
        if not home_existed:
            with contextlib.suppress(OSError):
                shutil.rmtree(home)
        raise
    return ProvisionResult(tenant_id=tenant_id, role=role, home=home, token=token)


def current_token(cfg: SvarogConfig, tenant_id: str) -> str | None:
    """Текущий gateway-token тенанта из его secrets.json."""
    return FileSecretStore(tenant_home(cfg, tenant_id) / "secrets.json").get(GATEWAY_TOKEN_REF)


def rotate_token(cfg: SvarogConfig, registry: TenantRegistry, tenant_id: str) -> str:
    """Выпустить новый bearer-token, отозвав прежний principal тенанта."""
    store = FileSecretStore(tenant_home(cfg, tenant_id) / "secrets.json")
    old = store.get(GATEWAY_TOKEN_REF)
    token = _issue_token()
    store.set(GATEWAY_TOKEN_REF, token)
    if old:
        registry.remove_principal(_gateway_principal(old))
    registry.add_principal(tenant_id, _gateway_principal(token))
    return token
