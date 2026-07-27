"""Override исполнителя, провайдера и модели для одного сообщения чата.

Выбор в поле ввода — свойство сообщения, а не правка `svarog.yaml`. Здесь
он превращается в производный конфиг: `model_copy(update=...)`, тот же
приём, что в `TaskRunner.spawn_child_run` (ADR-0016 фаза 3.5).

Модуль чистый: без БД, сети и файловой системы. Цены приходят снаружи
(их знает каталог моделей), чтобы это свойство сохранялось.
"""

from dataclasses import dataclass
from typing import Literal, Self

from svarog_harness.config.schema import SvarogConfig

# Ключ поддерева override в Run.meta.
OVERRIDE_META_KEY = "override"

ExecutorKind = Literal["native", "external"]


class OverrideError(Exception):
    """Override несовместим с конфигом; наружу уходит как HTTP 422."""


@dataclass(frozen=True)
class RunOverride:
    executor: ExecutorKind | None = None
    provider: str | None = None
    model: str | None = None

    def is_empty(self) -> bool:
        return self.executor is None and self.provider is None and self.model is None

    def to_meta(self) -> dict[str, str]:
        """Только заданные поля: пустые ключи в meta ничего не значат."""
        raw = {"executor": self.executor, "provider": self.provider, "model": self.model}
        return {key: value for key, value in raw.items() if value is not None}

    @classmethod
    def from_meta(cls, meta: dict[str, object] | None) -> Self:
        """Восстановить override из Run.meta; чужие ключи игнорируются.

        Терпимость намеренная: meta переживает обновления кода, и запись
        старого формата не должна ронять resume.
        """
        raw = (meta or {}).get(OVERRIDE_META_KEY)
        if not isinstance(raw, dict):
            return cls()
        executor = raw.get("executor")
        provider = raw.get("provider")
        model = raw.get("model")
        return cls(
            executor=executor if executor in ("native", "external") else None,
            provider=provider if isinstance(provider, str) else None,
            model=model if isinstance(model, str) else None,
        )


def apply_override(
    cfg: SvarogConfig,
    ov: RunOverride,
    *,
    prices: tuple[float, float] | None = None,
) -> SvarogConfig:
    """Производный конфиг сообщения. Исходный не мутируется.

    `prices` — (input, output) USD за миллион токенов выбранной модели.
    Без них учёт стоимости считал бы по ценам прошлой модели: они прибиты
    к записи провайдера, а не к модели.
    """
    if ov.is_empty() and prices is None:
        return cfg

    update: dict[str, object] = {}

    if ov.executor == "external":
        if cfg.executor.external is None:
            raise OverrideError(
                "внешний агент требует секцию executor.external в svarog.yaml "
                "(адаптер и образ sandbox, ADR-0016)"
            )
        if cfg.sandbox.type != "docker":
            raise OverrideError(
                f"внешний агент требует sandbox.type='docker', сейчас "
                f"'{cfg.sandbox.type}' (fail-closed, ADR-0016)"
            )
    if ov.executor is not None:
        update["executor"] = cfg.executor.model_copy(update={"type": ov.executor})

    target = ov.provider if ov.provider is not None else cfg.models.default
    if ov.provider is not None and ov.provider not in cfg.models.providers:
        known = ", ".join(sorted(cfg.models.providers)) or "нет"
        raise OverrideError(f"провайдер '{ov.provider}' не описан в models.providers (есть: {known})")

    provider_update: dict[str, object] = {}
    if ov.model is not None:
        provider_update["model"] = ov.model
    if prices is not None:
        provider_update["input_usd_per_mtok"] = prices[0]
        provider_update["output_usd_per_mtok"] = prices[1]

    if ov.provider is not None or provider_update:
        providers = dict(cfg.models.providers)
        if provider_update:
            providers[target] = providers[target].model_copy(update=provider_update)
        update["models"] = cfg.models.model_copy(
            update={"default": target, "providers": providers}
        )

    return cfg.model_copy(update=update)
