"""Чтение и атомарная запись project-config ``svarog.yaml``.

Общий для CLI (``svarog policies configure``) и TUI (``/set`` в чате). Модуль
не знает про Typer: бросает ProjectConfigError, вызывающий переводит его в
своё исключение — typer.BadParameter в CLI, SettingsApplyError в чате, где
typer-ошибка всплыла бы наружу трейсбеком.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


class ProjectConfigError(ValueError):
    """Конфиг проекта нечитаем: битый YAML или не mapping на верхнем уровне."""


def read_project_config(path: Path) -> dict[str, Any]:
    """Вернуть исходный project-config, не смешивая его с user-config."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProjectConfigError(f"невалидный YAML в {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ProjectConfigError(f"{path}: верхний уровень должен быть mapping")
    return data


def write_yaml(path: Path, data: Mapping[str, Any]) -> None:
    """Атомарно сохранить YAML, чтобы Ctrl+C не оставил усеченный конфиг."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    temporary.replace(path)
