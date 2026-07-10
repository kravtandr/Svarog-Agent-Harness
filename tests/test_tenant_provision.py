"""Фаза 2: провижн тенанта, Telegram-резолвинг, role re-clamp на resume."""

from pathlib import Path
from typing import Any

import pytest

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import SvarogConfig, TenantRole
from svarog_harness.gateway import TenantHub
from svarog_harness.gateway.telegram import TelegramBot, TelegramTransport
from svarog_harness.runtime.orchestrator import TaskRunner
from svarog_harness.tenant import (
    GATEWAY_TOKEN_REF,
    TenantExistsError,
    TenantRegistry,
    current_token,
    provision_tenant,
    rotate_token,
)
from svarog_harness.tenant.models import TenantContext


def _cfg(tmp_path: Path) -> SvarogConfig:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake\n"
        "sandbox:\n  type: local-trusted\n"
        f"tenancy:\n  enabled: true\n  home_root: {tmp_path / 'homes'}\n",
        encoding="utf-8",
    )
    return load_config(project_dir=ws)


def _registry(tmp_path: Path) -> TenantRegistry:
    return TenantRegistry(tmp_path / "tenants.json")


# --- провижн ------------------------------------------------------------------


async def test_provision_creates_home_and_token(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    reg = _registry(tmp_path)
    result = await provision_tenant(cfg, reg, "alice", TenantRole.STANDARD)

    home = (tmp_path / "homes" / "alice").resolve()
    assert result.home == home
    for sub in ("memory", "skills", "workspaces", "policies"):
        assert (home / sub).is_dir()
    # separate-git-dir (ADR-0015 §0.2): в дереве репозитория — файл-указатель
    # `.git`, объекты git вынесены в `<home>/.gitdirs/<repo>` вне дерева.
    assert (home / "memory" / ".git").is_file()
    assert (home / "skills" / ".git").is_file()
    assert (home / ".gitdirs" / "memory").is_dir()
    assert (home / ".gitdirs" / "skills").is_dir()
    assert (home / "svarog.db").is_file()
    # secrets.json 0600 с токеном
    secrets_file = home / "secrets.json"
    assert secrets_file.is_file()
    assert (secrets_file.stat().st_mode & 0o777) == 0o600
    # токен зарегистрирован как principal и лежит в secrets.json
    assert current_token(cfg, "alice") == result.token
    ctx = reg.resolve_principal(f"gateway:{result.token}")
    assert ctx is not None and ctx.tenant_id == "alice" and ctx.role is TenantRole.STANDARD


async def test_provision_duplicate_rejected(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    reg = _registry(tmp_path)
    await provision_tenant(cfg, reg, "alice", TenantRole.STANDARD)
    with pytest.raises(TenantExistsError):
        await provision_tenant(cfg, reg, "alice", TenantRole.SUPERUSER)


async def test_rotate_token_revokes_old(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    reg = _registry(tmp_path)
    result = await provision_tenant(cfg, reg, "alice", TenantRole.STANDARD)
    old = result.token
    new = rotate_token(cfg, reg, "alice")

    assert new != old
    assert current_token(cfg, "alice") == new
    assert reg.resolve_principal(f"gateway:{old}") is None  # старый отозван
    rotated = reg.resolve_principal(f"gateway:{new}")
    assert rotated is not None and rotated.tenant_id == "alice"
    assert GATEWAY_TOKEN_REF  # ref определён


# --- Telegram multi-tenant ----------------------------------------------------


class _FakeTx(TelegramTransport):
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        return []

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> None:
        self.sent.append({"chat_id": chat_id, "text": text})

    async def answer_callback(self, callback_id: str) -> None:
        pass


def _msg(user_id: int, text: str) -> dict[str, Any]:
    return {"message": {"chat": {"id": 100}, "from": {"id": user_id}, "text": text}}


async def test_telegram_from_hub_denies_unregistered(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    reg = _registry(tmp_path)
    reg.create("alice", TenantRole.STANDARD)
    reg.add_principal("alice", "telegram:42")
    hub = TenantHub(cfg, reg)
    tx = _FakeTx()
    bot = TelegramBot.from_hub(hub, reg, tx)

    await bot.handle_update(_msg(999, "сделай что-то"))  # незарегистрированный
    assert tx.sent == [{"chat_id": 100, "text": "⛔ Доступ запрещён."}]


async def test_telegram_from_hub_resolves_registered_user(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    reg = _registry(tmp_path)
    reg.create("alice", TenantRole.STANDARD)
    reg.add_principal("alice", "telegram:42")
    hub = TenantHub(cfg, reg)
    bot = TelegramBot.from_hub(hub, reg, _FakeTx())

    resolved = await bot._resolve(42)  # user → сервис тенанта alice
    assert resolved is hub.service_for(TenantContext("alice", TenantRole.STANDARD))
    assert await bot._resolve(999) is None  # неизвестный, provisioning=manual → отказ


async def test_first_touch_provisions_unknown_user(tmp_path: Path) -> None:
    # provisioning=first_touch: неизвестный пользователь авто-провижнится.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n  default: local\n  providers:\n    local:\n"
        "      base_url: http://localhost:9/v1\n      model: fake\n"
        f"tenancy:\n  enabled: true\n  provisioning: first_touch\n"
        f"  home_root: {tmp_path / 'homes'}\n",
        encoding="utf-8",
    )
    cfg = load_config(project_dir=ws)
    reg = _registry(tmp_path)
    hub = TenantHub(cfg, reg)
    bot = TelegramBot.from_hub(hub, reg, _FakeTx())

    svc = await bot._resolve(777)  # новый пользователь
    assert svc is not None
    assert reg.get("tg-777") is not None  # тенант создан
    ctx = reg.resolve_principal("telegram:777")
    assert ctx is not None and ctx.tenant_id == "tg-777"
    assert (tmp_path / "homes" / "tg-777" / "svarog.db").is_file()
    # повторный вызов идемпотентен (тенант уже есть)
    assert await bot._resolve(777) is svc


# --- role re-clamp (ADR-0013) -------------------------------------------------


def test_taskrunner_standard_clamps_local_trusted(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)  # base sandbox = local-trusted
    ws = tmp_path / "homes" / "alice" / "workspaces"
    standard = TaskRunner(cfg, ws, role=TenantRole.STANDARD)
    assert standard._cfg.sandbox.type == "docker"  # клампнут
    assert standard._cfg.secrets.env_fallback is False
    superuser = TaskRunner(cfg, ws, role=TenantRole.SUPERUSER)
    assert superuser._cfg.sandbox.type == "local-trusted"  # no-op
