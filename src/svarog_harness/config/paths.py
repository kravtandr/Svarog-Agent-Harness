"""Разрешение путей из конфигурации в абсолютные (§13).

Чистые функции без побочных эффектов: используются и CLI, и оркестратором
runtime, и gateway — чтобы каталоги skills/memory считались одинаково везде.

Здесь же живут резолвинг и кламп тенанта (ADR-0012/0013/0014): per-tenant
`SvarogConfig` строится переписыванием путей под home тенанта плюс принуждение
роли. Ядро (`TaskRunner`) параметризовано этим cfg и не меняется.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from svarog_harness.config.schema import SvarogConfig, TenantRole


def memory_dir(cfg: SvarogConfig) -> Path | None:
    """Каталог memory-репозитория (Flow A), если память включена в конфиге."""
    if cfg.memory.path is None:
        return None
    return cfg.memory.path.expanduser().resolve()


def skills_dirs(cfg: SvarogConfig, workspace: Path) -> list[Path]:
    """Абсолютные пути каталогов skills из конфигурации."""
    dirs = []
    for raw in cfg.skills.paths:
        path = raw.expanduser()
        if not path.is_absolute():
            path = workspace / path
        dirs.append(path.resolve())
    return dirs


def first_existing_skills_dir(cfg: SvarogConfig, workspace: Path) -> Path | None:
    """Первый существующий каталог skills — mount ro в sandbox (ADR-0002)."""
    for path in skills_dirs(cfg, workspace):
        if path.is_dir():
            return path
    return None


# --- мультитенантность: резолвинг + кламп роли (ADR-0012/0013/0014) -----------


class TenantConfinementError(Exception):
    """Резолвнутый путь тенанта вышел за пределы его home — нарушение изоляции."""


@dataclass(frozen=True)
class ResolvedTenant:
    """Готовый к запуску контекст тенанта: cfg с путями под home + роль.

    `TaskRunner(resolved.cfg, resolved.workspace)` дальше работает без изменений.
    """

    tenant_id: str
    role: TenantRole
    cfg: SvarogConfig
    workspace: Path


def tenant_home(cfg: SvarogConfig, tenant_id: str) -> Path:
    """Каталог agent-home тенанта: `tenancy.home_root/<id>/` (ADR-0012)."""
    return (cfg.tenancy.home_root.expanduser() / tenant_id).resolve()


def registry_path(cfg: SvarogConfig) -> Path:
    """Файл реестра тенантов — рядом с home_root (`agent-home/tenants.json`)."""
    return cfg.tenancy.home_root.expanduser().resolve().parent / "tenants.json"


def clamp_by_role(cfg: SvarogConfig, role: TenantRole) -> SvarogConfig:
    """Принуждение роли (ADR-0013). Кламп сильнее per-tenant yaml.

    Для `standard`: docker принудительно, сеть sandbox выключена, secret-scan
    включён, env-fallback секретов выключен — щели ослабить безопасность своим
    конфигом до старта run не остаётся. `superuser` — как настроено.
    """
    if role is not TenantRole.STANDARD:
        return cfg
    return cfg.model_copy(
        update={
            "sandbox": cfg.sandbox.model_copy(update={"type": "docker", "network": "disabled"}),
            "secrets": cfg.secrets.model_copy(update={"env_fallback": False}),
            "git": cfg.git.model_copy(update={"secret_scan_before_commit": True}),
            "verifier": cfg.verifier.model_copy(update={"secret_scan": True}),
            # MCP выключен для standard по умолчанию (ADR-0014 #8): внешний сервер —
            # выход за пределы sandbox; opt-in требует per-tenant конфига (Фаза 3).
            "mcp": cfg.mcp.model_copy(update={"servers": {}}),
        }
    )


def _tenant_owned_paths(cfg: SvarogConfig, workspace: Path) -> list[Path]:
    """Пути, которые ОБЯЗАНЫ лежать под home тенанта.

    Shared-ro скиллы (`skills.paths[1:]`) намеренно исключены — они общий слой
    вне home (ADR-0012 §5). Проверяется только tenant-writable слой `paths[0]`.
    """
    owned = [workspace, cfg.storage.db_path]
    if cfg.memory.path is not None:
        owned.append(cfg.memory.path)
    if cfg.secrets.path is not None:
        owned.append(cfg.secrets.path)
    if cfg.skills.paths:
        owned.append(cfg.skills.paths[0])
    return owned


def assert_confined(cfg: SvarogConfig, home: Path, workspace: Path) -> None:
    """Все tenant-owned пути (и workspace) строго под home; иначе — ошибка.

    `resolve()` раскрывает `..` и symlink'и, поэтому ссылка наружу home тоже
    отвергается. Защита от кривого home и от per-tenant yaml, уводящего пути
    за пределы тенанта (нарушение изоляции ADR-0012).
    """
    home_r = home.expanduser().resolve()
    for raw in _tenant_owned_paths(cfg, workspace):
        resolved = raw.expanduser().resolve()
        if resolved != home_r and not resolved.is_relative_to(home_r):
            raise TenantConfinementError(
                f"путь тенанта выходит за пределы home {home_r}: {resolved}"
            )


def resolve_tenant_config(
    base: SvarogConfig,
    *,
    tenant_id: str,
    home: Path,
    role: TenantRole,
    shared_skills: Sequence[Path] = (),
) -> ResolvedTenant:
    """Собрать per-tenant cfg: пути под home + кламп роли + проверка confinement.

    Память переносится в home только если она включена в base (глобальное
    выключение памяти уважается). Скиллы: tenant-writable слой `home/skills`
    плюс shared-ro слои (вне home). Секреты/БД/workspace — всегда под home.
    """
    home_r = home.expanduser().resolve()
    skills_paths: list[Path] = [home_r / "skills"]
    skills_paths.extend(p.expanduser().resolve() for p in shared_skills)

    updates: dict[str, object] = {
        "skills": base.skills.model_copy(update={"paths": skills_paths}),
        "storage": base.storage.model_copy(update={"db_path": home_r / "svarog.db"}),
        "secrets": base.secrets.model_copy(update={"path": home_r / "secrets.json"}),
    }
    if base.memory.path is not None:
        updates["memory"] = base.memory.model_copy(update={"path": home_r / "memory"})

    cfg = clamp_by_role(base.model_copy(update=updates), role)
    workspace = home_r / "workspaces"
    assert_confined(cfg, home_r, workspace)
    return ResolvedTenant(tenant_id=tenant_id, role=role, cfg=cfg, workspace=workspace)


def resolve_local_tenant(base: SvarogConfig, workspace: Path) -> ResolvedTenant:
    """Однотенантный режим (`tenancy.enabled=false`): неявный superuser-тенант.

    Пути base НЕ переписываются — поведение как до мультитенантности (ADR-0014).
    """
    return ResolvedTenant(
        tenant_id=base.tenancy.default_tenant,
        role=TenantRole.SUPERUSER,
        cfg=base,
        workspace=workspace.expanduser().resolve(),
    )
