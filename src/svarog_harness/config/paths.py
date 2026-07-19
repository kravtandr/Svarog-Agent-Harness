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


class WorkspaceLayoutError(Exception):
    """Control-plane (БД/память/скиллы) пересекается с workspace (ADR-0015 §0.3)."""


def _control_plane_paths(cfg: SvarogConfig, workspace: Path) -> list[tuple[str, Path]]:
    """Пути control-plane, которые ОБЯЗАНЫ быть непересекающимися с workspace.

    Только tenant-writable слой скиллов (`paths[0]`) — shared-ro слои общие и
    лежат вне home (ADR-0012 §5), их пересечение с workspace допустимо.
    """
    paths: list[tuple[str, Path]] = [("storage.db_path", cfg.storage.db_path)]
    if cfg.memory.path is not None:
        paths.append(("memory.path", cfg.memory.path))
    if cfg.skills.paths:
        paths.append(("skills.paths[0]", cfg.skills.paths[0]))
    return paths


def workspace_layout_violations(cfg: SvarogConfig, workspace: Path) -> list[str]:
    """Пересечения workspace с control-plane каталогами (ADR-0015 §0.3), без raise.

    cp внутри workspace — агент дотягивается до control-plane файлами/bash;
    workspace внутри cp — control-plane является предком рабочего дерева.
    """
    ws = workspace.expanduser().resolve()
    violations: list[str] = []
    for label, raw in _control_plane_paths(cfg, workspace):
        cp = raw.expanduser().resolve()
        if cp == ws or cp.is_relative_to(ws) or ws.is_relative_to(cp):
            violations.append(f"{label} ({cp}) пересекается с workspace ({ws})")
    return violations


def assert_workspace_isolated(
    cfg: SvarogConfig, workspace: Path, *, allow_overlap: bool = False
) -> None:
    """Workspace непересекается с control-plane каталогами (ADR-0015 §0.3).

    Инвариант раскладки: рабочая директория агента — строго отдельный каталог,
    в котором НЕ лежат ни БД, ни память, ни tenant-скиллы, и который сам не
    является их подкаталогом.

    Enforcement привязан к модели изоляции: в `docker` (все `standard`-тенанты
    заклампаны сюда, ADR-0013; их раскладка уже даёт disjoint через
    resolve_tenant_config) пересечение — ошибка конфигурации и run отклоняется.
    В `local-trusted` bash работает на хосте и путями не заперт в принципе —
    остаточный доступ к control-plane принят как явный trade-off режима
    «trusted» (§17): нарушения возвращаются как предупреждения, а не блокируют.

    allow_overlap — человек явно подтвердил пересечение в локальном CLI
    (ADR-0018): гейт пропускает. Флаг выставляется только интерактивным
    TTY-путём superuser'а; gateway/tenant-пути его не передают (fail-closed).
    """
    violations = workspace_layout_violations(cfg, workspace)
    if not violations:
        return
    detail = "; ".join(violations)
    if cfg.sandbox.type == "local-trusted":
        return  # документированный trade-off режима trusted — не блокируем
    if allow_overlap:
        return  # человек подтвердил доступ агента к control-plane (ADR-0018)
    raise WorkspaceLayoutError(
        f"control-plane пересекается с workspace: {detail}. "
        f"Держите БД/память/скиллы вне рабочего дерева агента (ADR-0015 §0.3): "
        f"запускайте run в отдельном каталоге (напр. workspaces/tasks/<run>/)"
    )


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
