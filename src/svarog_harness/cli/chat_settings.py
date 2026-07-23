"""Смена executor / mode / policies из chat + запись в project svarog.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from svarog_harness.cli.chat_display import MODE_CLOUD, MODE_LOCAL
from svarog_harness.common.project_config import (
    ProjectConfigError,
    read_project_config,
    write_yaml,
)
from svarog_harness.config.loader import PROJECT_CONFIG_NAME, deep_merge
from svarog_harness.config.schema import (
    AutonomyMode,
    ExecutorConfig,
    SvarogConfig,
)
from svarog_harness.scaffold import DEFAULT_CLAUDE_IMAGE, DEFAULT_OPENCODE_IMAGE

# Дефолтные образы per-adapter (те же, что `svarog init` пишет в свежий
# svarog.yaml). Если текущий image — один из этих дефолтов, свап adapter'а
# должен подтянуть за собой и его: иначе в sandbox остаётся CLI прежнего
# агента и запуск падает `command not found` (нет образа под codex — не трогаем).
_DEFAULT_EXTERNAL_IMAGES: dict[str, str] = {
    "claude-code": DEFAULT_CLAUDE_IMAGE,
    "opencode": DEFAULT_OPENCODE_IMAGE,
}


class SettingsApplyError(ValueError):
    """Нельзя применить выбор (нет секции external, неизвестный label и т.п.)."""


def patch_project_config(workspace: Path, patch: dict[str, Any]) -> Path:
    """Deep-merge patch в project ``svarog.yaml`` и атомарно сохранить."""
    path = workspace / PROJECT_CONFIG_NAME
    try:
        raw = read_project_config(path)
    except ProjectConfigError as exc:
        raise SettingsApplyError(str(exc)) from exc
    merged = deep_merge(raw, patch)
    write_yaml(path, merged)
    return path


def apply_executor_label(cfg: SvarogConfig, label: str) -> SvarogConfig:
    """Применить label вида ``native/docker`` или ``external/claude-code``."""
    kind, _, detail = label.partition("/")
    if kind == "native":
        if detail == "local":
            sandbox_type: Literal["docker", "local-trusted"] = "local-trusted"
        elif detail == "docker":
            sandbox_type = "docker"
        else:
            raise SettingsApplyError(f"неизвестный native sandbox: {label}")
        return cfg.model_copy(
            update={
                "executor": ExecutorConfig(type="native", external=cfg.executor.external),
                "sandbox": cfg.sandbox.model_copy(update={"type": sandbox_type}),
            }
        )
    if kind == "external":
        if not detail:
            raise SettingsApplyError(f"неполный executor: {label}")
        if cfg.executor.external is None:
            raise SettingsApplyError(
                "для external нужен executor.external в svarog.yaml (image и adapter)"
            )
        update: dict[str, Any] = {"adapter": detail}
        if cfg.executor.external.image in _DEFAULT_EXTERNAL_IMAGES.values():
            new_default = _DEFAULT_EXTERNAL_IMAGES.get(detail)
            if new_default is not None:
                update["image"] = new_default
        external = cfg.executor.external.model_copy(update=update)
        try:
            # Смена адаптера перевалидирует секцию (напр. wire=openai против
            # anthropic base_url) — падение конвертируем в SettingsApplyError,
            # чтобы chat-сессия пережила отклонённый выбор (S15c).
            executor = ExecutorConfig(type="external", external=external)
        except ValidationError as exc:
            raise SettingsApplyError(
                f"конфигурация external/{detail} невалидна: {exc.errors()[0]['msg']}"
            ) from exc
        return cfg.model_copy(update={"executor": executor})
    raise SettingsApplyError(f"неизвестный executor: {label}")


def apply_mode(cfg: SvarogConfig, mode: str) -> SvarogConfig:
    """``локальный loop`` → native; ``cloud-агент`` → external (нужна секция)."""
    if mode == MODE_LOCAL:
        return cfg.model_copy(
            update={
                "executor": ExecutorConfig(type="native", external=cfg.executor.external),
            }
        )
    if mode == MODE_CLOUD:
        if cfg.executor.external is None:
            raise SettingsApplyError(
                "cloud-агент требует executor.external в svarog.yaml (image и adapter)"
            )
        return cfg.model_copy(
            update={
                "executor": ExecutorConfig(type="external", external=cfg.executor.external),
            }
        )
    raise SettingsApplyError(f"неизвестный mode: {mode}")


def apply_policies(cfg: SvarogConfig, autonomy_value: str) -> tuple[SvarogConfig, AutonomyMode]:
    """Сменить runtime.autonomy (профиль policies берётся по имени режима)."""
    try:
        autonomy = AutonomyMode(autonomy_value)
    except ValueError as exc:
        raise SettingsApplyError(f"неизвестная автономия: {autonomy_value}") from exc
    runtime = cfg.runtime.model_copy(update={"autonomy": autonomy})
    return cfg.model_copy(update={"runtime": runtime}), autonomy


def executor_yaml_patch(cfg: SvarogConfig) -> dict[str, Any]:
    """Фрагмент YAML для текущего executor + sandbox."""
    patch: dict[str, Any] = {
        "executor": {"type": cfg.executor.type},
        "sandbox": {"type": cfg.sandbox.type},
    }
    if cfg.executor.external is not None:
        ext = cfg.executor.external
        external: dict[str, Any] = {
            "adapter": ext.adapter,
            "image": ext.image,
            "auth": ext.auth,
        }
        if ext.model is not None:
            external["model"] = ext.model
        if ext.api_key_ref is not None:
            external["api_key_ref"] = ext.api_key_ref
        if ext.oauth_token_ref is not None:
            external["oauth_token_ref"] = ext.oauth_token_ref
        patch["executor"]["external"] = external
    return patch


def policies_yaml_patch(autonomy: AutonomyMode) -> dict[str, Any]:
    return {"runtime": {"autonomy": autonomy.value}}
