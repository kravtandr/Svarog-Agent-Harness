"""Интерактивное редактирование policy-профилей в project ``svarog.yaml``."""

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import ValidationError

from svarog_harness.config.loader import PROJECT_CONFIG_NAME
from svarog_harness.config.schema import AutonomyMode, PoliciesConfig

policies_app = typer.Typer(
    help="Policy-профили: интерактивная настройка require_approval и notify.",
    no_args_is_help=True,
)

_PROFILE_NAMES = tuple(mode.value for mode in AutonomyMode)


def _read_project_config(path: Path) -> dict[str, Any]:
    """Вернуть исходный project-config, не смешивая его с user-config."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise typer.BadParameter(f"невалидный YAML в {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise typer.BadParameter(f"{path}: верхний уровень должен быть mapping")
    return data


def _patterns(value: str) -> list[str]:
    """Разобрать ввод вида ``file.write, git.push`` в список glob-паттернов."""
    return [pattern.strip() for pattern in value.split(",") if pattern.strip()]


def _prompt_patterns(label: str, current: list[str]) -> list[str]:
    value = typer.prompt(label, default=", ".join(current), show_default=bool(current))
    return _patterns(value)


def _write_yaml(path: Path, data: Mapping[str, Any]) -> None:
    """Атомарно сохранить YAML, чтобы Ctrl+C не оставил усеченный конфиг."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    temporary.replace(path)


@policies_app.command("configure")
def configure(
    workspace: Annotated[
        Path | None, typer.Option("--workspace", "-w", help="Каталог проекта (по умолчанию cwd)")
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", "-p", help="Профиль: supervised, auto или yolo")
    ] = None,
) -> None:
    """Настроить списки require_approval и notify для policy-профиля."""
    workspace = (workspace or Path.cwd()).resolve()
    if not workspace.is_dir():
        typer.echo(f"Ошибка: workspace не существует: {workspace}", err=True)
        raise typer.Exit(code=1)

    config_path = workspace / PROJECT_CONFIG_NAME
    raw_config = _read_project_config(config_path)
    raw_policies = raw_config.get("policies", {})
    if not isinstance(raw_policies, dict):
        raise typer.BadParameter("policies должен быть mapping")
    raw_profiles = raw_policies.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise typer.BadParameter("policies.profiles должен быть mapping")

    if profile is None:
        profile = typer.prompt("Профиль (supervised, auto, yolo)", default="yolo")
    if profile not in _PROFILE_NAMES:
        typer.echo("Ошибка: профиль должен быть supervised, auto или yolo.", err=True)
        raise typer.Exit(code=1)

    existing = raw_profiles.get(profile, {})
    if not isinstance(existing, dict):
        raise typer.BadParameter(f"policies.profiles.{profile} должен быть mapping")
    current_approval = existing.get("require_approval", [])
    current_notify = existing.get("notify", [])
    if (
        not isinstance(current_approval, list)
        or not isinstance(current_notify, list)
        or not all(isinstance(pattern, str) for pattern in current_approval + current_notify)
    ):
        raise typer.BadParameter(f"policies.profiles.{profile}: списки должны быть YAML-массивами")

    typer.echo("Укажите glob-паттерны action type через запятую; пустой ввод очищает список.")
    require_approval = _prompt_patterns("Require approval", current_approval)
    while True:
        notify = _prompt_patterns("Notify", current_notify)
        overlap = sorted(set(require_approval) & set(notify))
        if not overlap:
            break
        typer.echo(
            "Один action не может одновременно быть в require_approval и notify: "
            + ", ".join(overlap),
            err=True,
        )

    updated_policies = dict(raw_policies)
    updated_profiles = dict(raw_profiles)
    updated_profiles[profile] = {
        "require_approval": require_approval,
        "notify": notify,
    }
    updated_policies["profiles"] = updated_profiles
    try:
        PoliciesConfig.model_validate(updated_policies)
    except ValidationError as exc:
        raise typer.BadParameter(f"некорректная policy-конфигурация: {exc}") from exc

    typer.echo(f"\nПрофиль {profile}:")
    typer.echo(f"  require_approval: {require_approval or '[]'}")
    typer.echo(f"  notify: {notify or '[]'}")
    typer.echo("Critical-действия по-прежнему требуют approval и не настраиваются здесь.")
    if not typer.confirm(f"Сохранить в {config_path}?", default=True):
        typer.echo("Изменения не сохранены.")
        return

    raw_config["policies"] = updated_policies
    _write_yaml(config_path, raw_config)
    typer.echo(f"Policy-профиль '{profile}' сохранён в {config_path}.")
