"""Варианты исполнителя для селекта поля ввода.

Тот же принцип, что у `cli/chat_display.chat_status_view`: native всегда,
плюс каждый адаптер, который прописан в конфиге или чей CLI нашёлся в PATH.
Недоступные не прячем — иначе человек не понимает, почему в списке нет
codex, и думает, что Сварог его не умеет.
"""

from dataclasses import dataclass
from typing import Literal

from svarog_harness.config.schema import SvarogConfig
from svarog_harness.runtime.agents import EXTERNAL_ADAPTERS, adapter_available


@dataclass(frozen=True)
class ExecutorOption:
    value: str
    kind: Literal["native", "external"]
    adapter: str | None
    available: bool
    is_active: bool


def executor_options(cfg: SvarogConfig) -> list[ExecutorOption]:
    configured = cfg.executor.external.adapter if cfg.executor.external is not None else None
    native_active = cfg.executor.type == "native"
    options = [
        ExecutorOption(
            value="native", kind="native", adapter=None, available=True, is_active=native_active
        )
    ]
    for adapter in EXTERNAL_ADAPTERS:
        options.append(
            ExecutorOption(
                value=adapter,
                kind="external",
                adapter=adapter,
                available=adapter == configured or adapter_available(adapter),
                is_active=not native_active and adapter == configured,
            )
        )
    return options
