"""Варианты исполнителя и sandbox для селектов поля ввода.

Тот же принцип, что у `cli/chat_display.chat_status_view`: native всегда,
плюс каждый адаптер, который прописан в конфиге, чей CLI нашёлся в PATH или
чей docker-образ собран локально. Недоступные не прячем — иначе человек не
понимает, почему в списке нет codex, и думает, что Сварог его не умеет.

Детекция по образу обязательна: внешний агент исполняется В КОНТЕЙНЕРЕ
(ADR-0016), host-CLI ему не нужен — до 31.07.2026 собранный
svarog/agent-opencode:latest показывался «недоступным» только потому, что
`opencode` не стоял на хосте.
"""

import subprocess
from dataclasses import dataclass
from typing import Literal

from svarog_harness.config.schema import SvarogConfig
from svarog_harness.runtime.agents import EXTERNAL_ADAPTERS, adapter_available
from svarog_harness.sandbox import find_docker
from svarog_harness.scaffold import DEFAULT_CLAUDE_IMAGE, DEFAULT_OPENCODE_IMAGE

# Дефолтные образы per-adapter — те же, что пишет svarog init и подменяет
# override адаптера (gateway/overrides.py). У codex образа в проекте нет.
_ADAPTER_IMAGES: dict[str, str] = {
    "claude-code": DEFAULT_CLAUDE_IMAGE,
    "opencode": DEFAULT_OPENCODE_IMAGE,
}


@dataclass(frozen=True)
class ExecutorOption:
    value: str
    kind: Literal["native", "external"]
    adapter: str | None
    available: bool
    is_active: bool


@dataclass(frozen=True)
class SandboxOption:
    """Вариант sandbox для селекта поля ввода (зеркало ExecutorOption)."""

    value: Literal["docker", "local-trusted"]
    available: bool
    is_active: bool


def _image_present(image: str) -> bool:
    """Собран ли docker-образ локально; любой сбой — «нет» (доступность не врёт)."""
    docker = find_docker()
    if docker is None:
        return False
    try:
        probe = subprocess.run(
            [docker, "image", "inspect", image],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _adapter_image(cfg: SvarogConfig, adapter: str, configured: str | None) -> str | None:
    """Образ, в котором исполнялся бы адаптер: из конфига или дефолтный."""
    if adapter == configured and cfg.executor.external is not None:
        return cfg.executor.external.image
    return _ADAPTER_IMAGES.get(adapter)


def executor_options(cfg: SvarogConfig) -> list[ExecutorOption]:
    configured = cfg.executor.external.adapter if cfg.executor.external is not None else None
    native_active = cfg.executor.type == "native"
    options = [
        ExecutorOption(
            value="native", kind="native", adapter=None, available=True, is_active=native_active
        )
    ]
    for adapter in EXTERNAL_ADAPTERS:
        image = _adapter_image(cfg, adapter, configured)
        options.append(
            ExecutorOption(
                value=adapter,
                kind="external",
                adapter=adapter,
                available=(
                    adapter == configured
                    or adapter_available(adapter)
                    or (image is not None and _image_present(image))
                ),
                is_active=not native_active and adapter == configured,
            )
        )
    return options


def sandbox_options(cfg: SvarogConfig) -> list[SandboxOption]:
    """Варианты sandbox: docker доступен при наличии runtime, local-trusted всегда.

    Недоступный docker не прячем — с подсказкой человек понимает, что надо
    поднять Docker/Podman, а не что Сварог изоляцию не умеет.
    """
    active = cfg.sandbox.type
    return [
        SandboxOption(
            value="docker", available=find_docker() is not None, is_active=active == "docker"
        ),
        SandboxOption(value="local-trusted", available=True, is_active=active == "local-trusted"),
    ]
