"""Загрузка `svarog.yaml`: user-уровень + project-уровень + env (§13 TASK.md).

Порядок приоритета (от низшего к высшему):
user (`~/.svarog/svarog.yaml`) → project (`<project>/svarog.yaml`) → `SVAROG_*` env.
Файлы объединяются рекурсивно (deep merge): project-файл, задающий один ключ
секции, не затирает остальную секцию из user-файла.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from svarog_harness.config.schema import SvarogConfig

USER_CONFIG_PATH = Path("~/.svarog/svarog.yaml")
PROJECT_CONFIG_NAME = "svarog.yaml"


class ConfigError(Exception):
    """Ошибка загрузки или валидации конфигурации, с человекочитаемым текстом."""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Рекурсивно наложить `override` на `base`; словари объединяются, остальное заменяется."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Невалидный YAML в {path}:\n{exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: верхний уровень должен быть mapping, а не {type(raw).__name__}")
    return raw


def _format_validation_error(exc: ValidationError, sources: list[Path]) -> str:
    lines = [f"Конфигурация невалидна ({exc.error_count()} ошибок):"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<корень>"
        lines.append(f"  - {location}: {error['msg']}")
    if sources:
        lines.append("Файлы: " + ", ".join(str(p) for p in sources))
    else:
        lines.append(
            f"Файлы конфигурации не найдены (искали {USER_CONFIG_PATH} и ./{PROJECT_CONFIG_NAME})."
        )
    return "\n".join(lines)


def load_config(
    project_dir: Path | None = None,
    user_config_path: Path | None = None,
) -> SvarogConfig:
    """Собрать конфигурацию из user- и project-файлов; env-переменные поверх.

    Оба файла опциональны, но результат обязан пройти валидацию схемы
    (минимум — секция `models`). Все проблемы поднимаются как ConfigError.
    """
    user_path = (user_config_path or USER_CONFIG_PATH).expanduser()
    project_path = (project_dir or Path.cwd()) / PROJECT_CONFIG_NAME

    merged: dict[str, Any] = {}
    sources: list[Path] = []
    for path in (user_path, project_path):
        if path.is_file():
            merged = deep_merge(merged, _read_yaml_mapping(path))
            sources.append(path)

    try:
        return SvarogConfig(**merged)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, sources)) from exc
