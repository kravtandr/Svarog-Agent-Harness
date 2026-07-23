"""Хелперы, общие для команд CLI.

Вынесены из main.py при расщеплении по sub-app'ам: каждая группа команд
живёт в своём модуле (паттерн cli/policies.py), а общее — здесь, чтобы
модули групп не импортировали main.py и не создавали цикл импорта.
"""

from pathlib import Path

import typer
from rich.console import Console

from svarog_harness.config.loader import ConfigError, load_config
from svarog_harness.config.schema import AutonomyMode, SvarogConfig

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
