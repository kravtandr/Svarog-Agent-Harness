"""Инварианты мультитенантности Фазы 1 (ADR-0012/0013/0014).

Покрывает резолвинг per-tenant cfg, кламп роли, confinement путей, mount-scope
(память/секреты/БД — вне workspace), фикс env-leak и control-plane реестр.
"""

from pathlib import Path

import pytest

from svarog_harness.config.loader import load_config
from svarog_harness.config.paths import (
    ResolvedTenant,
    TenantConfinementError,
    WorkspaceLayoutError,
    assert_confined,
    assert_workspace_isolated,
    clamp_by_role,
    resolve_local_tenant,
    resolve_tenant_config,
    tenant_home,
    workspace_layout_violations,
)
from svarog_harness.config.schema import SvarogConfig, TenantRole
from svarog_harness.runtime.orchestrator import TaskRunner
from svarog_harness.secrets import default_secret_store
from svarog_harness.tenant import (
    PrincipalConflictError,
    TenantExistsError,
    TenantRegistry,
    TenantRegistryError,
)

_MINIMAL = """\
models:
  default: local
  providers:
    local:
      base_url: http://localhost:8000/v1
      model: qwen
"""


def _base_cfg(tmp_path: Path, extra: str = "") -> SvarogConfig:
    (tmp_path / "svarog.yaml").write_text(_MINIMAL + extra, encoding="utf-8")
    user = tmp_path / "user.yaml"
    return load_config(project_dir=tmp_path, user_config_path=user)


# --- резолвинг путей ----------------------------------------------------------


def test_resolve_puts_owned_paths_under_home(tmp_path: Path) -> None:
    base = _base_cfg(tmp_path, "memory:\n  path: ~/.svarog/memory\n")
    home = tmp_path / "tenants" / "alice"
    resolved = resolve_tenant_config(base, tenant_id="alice", home=home, role=TenantRole.STANDARD)
    home_r = home.resolve()
    assert resolved.cfg.storage.db_path == home_r / "svarog.db"
    assert resolved.cfg.secrets.path == home_r / "secrets.json"
    assert resolved.cfg.memory.path == home_r / "memory"
    assert resolved.cfg.skills.paths[0] == home_r / "skills"
    assert resolved.workspace == home_r / "workspaces"


def test_shared_skills_layered_after_tenant_dir(tmp_path: Path) -> None:
    base = _base_cfg(tmp_path)
    home = tmp_path / "tenants" / "bob"
    shared = tmp_path / "shared-skills"
    resolved = resolve_tenant_config(
        base, tenant_id="bob", home=home, role=TenantRole.STANDARD, shared_skills=[shared]
    )
    assert resolved.cfg.skills.paths == [home.resolve() / "skills", shared.resolve()]


def test_memory_stays_disabled_if_base_disabled(tmp_path: Path) -> None:
    base = _base_cfg(tmp_path)  # memory.path по умолчанию None
    resolved = resolve_tenant_config(
        base, tenant_id="c", home=tmp_path / "t" / "c", role=TenantRole.STANDARD
    )
    assert resolved.cfg.memory.path is None


def test_mount_scope_owned_paths_are_outside_workspace(tmp_path: Path) -> None:
    # Критичный инвариант: monтируя workspace, нельзя раскрыть память/секреты/БД.
    base = _base_cfg(tmp_path, "memory:\n  path: ~/.svarog/memory\n")
    home = tmp_path / "tenants" / "d"
    r = resolve_tenant_config(base, tenant_id="d", home=home, role=TenantRole.STANDARD)
    ws = r.workspace
    for sensitive in (r.cfg.storage.db_path, r.cfg.secrets.path, r.cfg.memory.path):
        assert sensitive is not None
        assert not sensitive.is_relative_to(ws)


# --- 0.3 (ADR-0015): раскладка workspace vs control-plane ---------------------


def test_resolved_tenant_layout_passes_isolation(tmp_path: Path) -> None:
    base = _base_cfg(tmp_path, "memory:\n  path: ~/.svarog/memory\n")
    r = resolve_tenant_config(
        base, tenant_id="a", home=tmp_path / "t" / "a", role=TenantRole.STANDARD
    )
    # docker-раскладка тенанта disjoint — check не падает.
    assert workspace_layout_violations(r.cfg, r.workspace) == []
    assert_workspace_isolated(r.cfg, r.workspace)


def test_docker_control_plane_inside_workspace_rejected(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    base = _base_cfg(
        tmp_path,
        "sandbox:\n  type: docker\n"
        f"memory:\n  path: {ws / 'memory'}\n"
        f"storage:\n  db_path: {ws / '.state' / 'svarog.db'}\n",
    )
    with pytest.raises(WorkspaceLayoutError):
        assert_workspace_isolated(base, ws)


def test_local_trusted_overlap_is_documented_tradeoff(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    base = _base_cfg(
        tmp_path,
        f"sandbox:\n  type: local-trusted\nmemory:\n  path: {ws / 'memory'}\n",
    )
    # Пересечение есть, но в local-trusted оно принято как trade-off — не падаем.
    assert workspace_layout_violations(base, ws)
    assert_workspace_isolated(base, ws)


def test_overlap_allowed_after_human_confirmation(tmp_path: Path) -> None:
    # ADR-0018: подтверждённое человеком пересечение пропускает гейт docker-раскладки.
    ws = tmp_path / "ws"
    base = _base_cfg(
        tmp_path,
        f"sandbox:\n  type: docker\nmemory:\n  path: {ws / 'memory'}\n",
    )
    with pytest.raises(WorkspaceLayoutError):
        assert_workspace_isolated(base, ws)
    assert_workspace_isolated(base, ws, allow_overlap=True)


def test_standard_role_clamps_layout_overlap_flag(tmp_path: Path) -> None:
    # Флаг подтверждения — только для superuser CLI; у standard-тенанта гейт
    # раскладки безусловный (fail-closed), даже если флаг передан.
    from svarog_harness.runtime.orchestrator import TaskRunner

    ws = tmp_path / "ws"
    ws.mkdir()
    base = _base_cfg(tmp_path, "sandbox:\n  type: docker\n")
    standard = TaskRunner(base, ws, role=TenantRole.STANDARD, allow_layout_overlap=True)
    superuser = TaskRunner(base, ws, allow_layout_overlap=True)
    assert standard._allow_layout_overlap is False
    assert superuser._allow_layout_overlap is True


# --- кламп роли ---------------------------------------------------------------


def test_clamp_standard_forces_hardening(tmp_path: Path) -> None:
    base = _base_cfg(
        tmp_path,
        "sandbox:\n  type: local-trusted\ngit:\n  secret_scan_before_commit: false\n"
        "verifier:\n  secret_scan: false\n",
    )
    clamped = clamp_by_role(base, TenantRole.STANDARD)
    assert clamped.sandbox.type == "docker"
    assert clamped.sandbox.network == "disabled"
    assert clamped.secrets.env_fallback is False
    assert clamped.git.secret_scan_before_commit is True
    assert clamped.verifier.secret_scan is True


def test_clamp_superuser_is_noop(tmp_path: Path) -> None:
    base = _base_cfg(tmp_path, "sandbox:\n  type: local-trusted\n")
    assert clamp_by_role(base, TenantRole.SUPERUSER) is base
    assert clamp_by_role(base, TenantRole.SUPERUSER).sandbox.type == "local-trusted"


def test_standard_yaml_cannot_escape_clamp(tmp_path: Path) -> None:
    # Per-tenant yaml пытается вернуть local-trusted — кламп сильнее.
    base = _base_cfg(tmp_path, "sandbox:\n  type: local-trusted\n")
    r = resolve_tenant_config(base, tenant_id="e", home=tmp_path / "e", role=TenantRole.STANDARD)
    assert r.cfg.sandbox.type == "docker"


# --- confinement --------------------------------------------------------------


def test_assert_confined_rejects_escape(tmp_path: Path) -> None:
    base = _base_cfg(tmp_path)
    home = (tmp_path / "home").resolve()
    home.mkdir()
    # db_path уводим наружу home — должно упасть.
    bad = base.model_copy(
        update={"storage": base.storage.model_copy(update={"db_path": tmp_path / "outside.db"})}
    )
    with pytest.raises(TenantConfinementError):
        assert_confined(bad, home, home / "workspaces")


def test_assert_confined_rejects_symlink_escape(tmp_path: Path) -> None:
    base = _base_cfg(tmp_path)
    home = (tmp_path / "home2").resolve()
    home.mkdir()
    outside = (tmp_path / "elsewhere").resolve()
    outside.mkdir()
    link = home / "sneaky"
    link.symlink_to(outside)  # symlink наружу home
    bad = base.model_copy(
        update={"storage": base.storage.model_copy(update={"db_path": link / "svarog.db"})}
    )
    with pytest.raises(TenantConfinementError):
        assert_confined(bad, home, home / "workspaces")


# --- локальный (однотенантный) режим ------------------------------------------


def test_resolve_local_tenant_keeps_base(tmp_path: Path) -> None:
    base = _base_cfg(tmp_path, "sandbox:\n  type: local-trusted\n")
    ws = tmp_path / "ws"
    r = resolve_local_tenant(base, ws)
    assert isinstance(r, ResolvedTenant)
    assert r.tenant_id == "local"
    assert r.role is TenantRole.SUPERUSER
    assert r.cfg is base  # пути не переписаны
    assert r.cfg.sandbox.type == "local-trusted"


def test_tenant_home_layout(tmp_path: Path) -> None:
    base = _base_cfg(tmp_path, f"tenancy:\n  home_root: {tmp_path / 'homes'}\n")
    assert tenant_home(base, "alice") == (tmp_path / "homes" / "alice").resolve()


# --- env-leak фикс ------------------------------------------------------------


def test_env_fallback_off_blocks_host_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_HOST_SECRET", "leaked")
    off = default_secret_store(None, env_fallback=False)
    assert off.get("SOME_HOST_SECRET") is None
    on = default_secret_store(None, env_fallback=True)
    assert on.get("SOME_HOST_SECRET") == "leaked"


def test_standard_clamp_disables_env_fallback_end_to_end(tmp_path: Path) -> None:
    base = _base_cfg(tmp_path)
    r = resolve_tenant_config(base, tenant_id="f", home=tmp_path / "f", role=TenantRole.STANDARD)
    assert r.cfg.secrets.env_fallback is False


# --- два секрет-скоупа (ADR-0014 #2) и MCP off (#8) ---------------------------


def test_clamp_standard_disables_mcp(tmp_path: Path) -> None:
    base = _base_cfg(tmp_path, "mcp:\n  servers:\n    foo:\n      command: echo\n")
    assert clamp_by_role(base, TenantRole.STANDARD).mcp.servers == {}
    assert clamp_by_role(base, TenantRole.SUPERUSER).mcp.servers != {}  # superuser сохраняет


def test_standard_two_secret_scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_PROVIDER_KEY", "sk-global")
    base = _base_cfg(tmp_path)
    r = resolve_tenant_config(base, tenant_id="p", home=tmp_path / "p", role=TenantRole.STANDARD)
    runner = TaskRunner(r.cfg, r.workspace, role=TenantRole.STANDARD)
    # host-скоуп резолвит глобальный env-ключ (провайдер вызывается host-side);
    # sandbox-скоуп — нет (env-fallback выключен клампом).
    assert runner.host_store.get("MY_PROVIDER_KEY") == "sk-global"
    assert runner.store.get("MY_PROVIDER_KEY") is None


# --- реестр -------------------------------------------------------------------


def _registry(tmp_path: Path) -> TenantRegistry:
    return TenantRegistry(tmp_path / "tenants.json")


def test_registry_create_and_get(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    rec = reg.create("alice", TenantRole.STANDARD)
    assert rec.tenant_id == "alice"
    fetched = reg.get("alice")
    assert fetched is not None and fetched.role is TenantRole.STANDARD
    assert reg.get("missing") is None


def test_registry_duplicate_rejected(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create("alice", TenantRole.STANDARD)
    with pytest.raises(TenantExistsError):
        reg.create("alice", TenantRole.SUPERUSER)


def test_registry_principal_resolution(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create("alice", TenantRole.STANDARD)
    reg.add_principal("alice", "telegram:123")
    ctx = reg.resolve_principal("telegram:123")
    assert ctx is not None
    assert ctx.tenant_id == "alice"
    assert ctx.role is TenantRole.STANDARD
    assert reg.resolve_principal("telegram:999") is None


def test_registry_principal_conflict(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create("alice", TenantRole.STANDARD)
    reg.create("bob", TenantRole.STANDARD)
    reg.add_principal("alice", "telegram:1")
    with pytest.raises(PrincipalConflictError):
        reg.add_principal("bob", "telegram:1")


def test_registry_add_principal_unknown_tenant(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    with pytest.raises(TenantRegistryError):
        reg.add_principal("ghost", "telegram:1")


def test_registry_run_index_roundtrip(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create("alice", TenantRole.STANDARD)
    reg.record_run("run-abc", "alice")
    assert reg.tenant_of_run("run-abc") == "alice"
    assert reg.tenant_of_run("run-xyz") is None


def test_registry_persists_across_instances(tmp_path: Path) -> None:
    _registry(tmp_path).create("alice", TenantRole.SUPERUSER)
    reloaded = _registry(tmp_path)
    fetched = reloaded.get("alice")
    assert fetched is not None and fetched.role is TenantRole.SUPERUSER
    assert [r.tenant_id for r in reloaded.list_tenants()] == ["alice"]
