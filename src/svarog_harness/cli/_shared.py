"""Хелперы, общие для команд CLI.

Вынесены из main.py при расщеплении по sub-app'ам: каждая группа команд
живёт в своём модуле (паттерн cli/policies.py), а общее — здесь, чтобы
модули групп не импортировали main.py и не создавали цикл импорта.
"""

import json
from pathlib import Path

import typer
from rich.console import Console

from svarog_harness.config.loader import ConfigError, load_config
from svarog_harness.config.schema import AutonomyMode, SvarogConfig
from svarog_harness.secrets import SecretStore, selected_values
from svarog_harness.storage.models import Approval

console = Console()


def load_config_or_exit(project_dir: Path | None = None) -> SvarogConfig:
    try:
        return load_config(project_dir=project_dir)
    except ConfigError as exc:
        console.print(f"[red]ошибка конфигурации:[/red] {exc}")
        raise typer.Exit(code=1) from None


def resolve_autonomy(
    cfg: SvarogConfig, *, yolo: bool, auto: bool, supervised: bool
) -> AutonomyMode:
    flags = {
        AutonomyMode.YOLO: yolo,
        AutonomyMode.AUTO: auto,
        AutonomyMode.SUPERVISED: supervised,
    }
    chosen = [mode for mode, enabled in flags.items() if enabled]
    if len(chosen) > 1:
        console.print("[red]флаги --yolo/--auto/--supervised взаимоисключающие[/red]")
        raise typer.Exit(code=1)
    return chosen[0] if chosen else cfg.runtime.autonomy


def known_secret_values(cfg: SvarogConfig, store: SecretStore) -> frozenset[str]:
    """Значения секретов, которые нельзя выпускать в trace/коммиты (ADR-0006)."""
    refs = list(cfg.secrets.inject)
    refs.extend(
        provider.api_key_ref
        for provider in cfg.models.providers.values()
        if provider.api_key_ref is not None
    )
    for server in cfg.mcp.servers.values():
        refs.extend(server.env_refs)
    if cfg.gateway.token_ref is not None:
        refs.append(cfg.gateway.token_ref)
    if cfg.telegram.token_ref is not None:
        refs.append(cfg.telegram.token_ref)
    return store.values() | selected_values(store, refs)


def show_approval(approval: Approval) -> None:
    """Показать фактическое действие (команду/аргументы), не пересказ (§12)."""
    payload = approval.payload or {}
    console.print(f"[bold]approval {approval.id[:8]}[/bold] | run {approval.run_id[:8]}")
    console.print(f"  действие: {approval.action_type}")
    if payload.get("tool"):
        console.print(f"  tool: {payload['tool']}")
    if payload.get("arguments"):
        console.print(
            f"  аргументы: {json.dumps(payload['arguments'], ensure_ascii=False, indent=2)}"
        )
    if payload.get("reason"):
        console.print(f"  причина: {payload['reason']}")
