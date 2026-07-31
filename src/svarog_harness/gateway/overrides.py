"""Override исполнителя, провайдера и модели для одного сообщения чата.

Выбор в поле ввода — свойство сообщения, а не правка `svarog.yaml`. Здесь
он превращается в производный конфиг: `model_copy(update=...)`, тот же
приём, что в `TaskRunner.spawn_child_run` (ADR-0016 фаза 3.5).

Модуль чистый: без БД, сети и файловой системы. Цены приходят снаружи
(их знает каталог моделей), чтобы это свойство сохранялось.
"""

from dataclasses import dataclass
from typing import Literal, Self

from pydantic import ValidationError

from svarog_harness.config.schema import ExternalExecutorConfig, SvarogConfig
from svarog_harness.runtime.agents import EXTERNAL_ADAPTERS
from svarog_harness.scaffold import DEFAULT_CLAUDE_IMAGE, DEFAULT_OPENCODE_IMAGE

# Адаптеры с openai-совместимым LLM-трафиком: им нужен base_url/model/api-key,
# а не anthropic-дефолты секции, заточенной под claude-code.
_OPENAI_WIRE_ADAPTERS = frozenset({"opencode", "codex"})

# Ключ поддерева override в Run.meta.
OVERRIDE_META_KEY = "override"
# Ключ разрешённых цен модели в Run.meta (финал ревью, задача 2). Цены не
# входят в security-дайджест (ADR-0015 §0.4) — их персистентность в meta
# нужна не ради resume-гейта, а ради самого учёта стоимости: без неё resume
# пересчитывал бы их заново через каталог провайдера, и недоступность
# каталога (TTL истёк, write_config очистил кэш, провайдер лёг) молча
# откатывала бы run на цены из svarog.yaml для другой модели.
PRICES_META_KEY = "override_prices"

ExecutorKind = Literal["native", "external"]


class OverrideError(Exception):
    """Override несовместим с конфигом; наружу уходит как HTTP 422."""


SandboxKind = Literal["docker", "local-trusted"]


@dataclass(frozen=True)
class RunOverride:
    executor: ExecutorKind | None = None
    provider: str | None = None
    model: str | None = None
    adapter: str | None = None
    sandbox: SandboxKind | None = None

    def is_empty(self) -> bool:
        return (
            self.executor is None
            and self.provider is None
            and self.model is None
            and self.adapter is None
            and self.sandbox is None
        )

    def to_meta(self) -> dict[str, str]:
        """Только заданные поля: пустые ключи в meta ничего не значат."""
        raw = {
            "executor": self.executor,
            "provider": self.provider,
            "model": self.model,
            "adapter": self.adapter,
            "sandbox": self.sandbox,
        }
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
        adapter = raw.get("adapter")
        sandbox = raw.get("sandbox")
        return cls(
            executor=executor if executor in ("native", "external") else None,
            provider=provider if isinstance(provider, str) else None,
            model=model if isinstance(model, str) else None,
            adapter=adapter if adapter in EXTERNAL_ADAPTERS else None,
            sandbox=sandbox if sandbox in ("docker", "local-trusted") else None,
        )


# Образы per-adapter: те же дефолты, что пишет `svarog init`. Подменяем
# образ вместе с адаптером, иначе в sandbox остаётся CLI прежнего агента и
# запуск падает `command not found`. Кастомный образ не трогаем — его
# поставили руками, и подмена молча увела бы запуск в другой контейнер.
_ADAPTER_IMAGES: dict[str, str] = {
    "claude-code": DEFAULT_CLAUDE_IMAGE,
    "opencode": DEFAULT_OPENCODE_IMAGE,
}


def prices_to_meta(prices: tuple[float, float] | None) -> dict[str, float] | None:
    """Цены для записи в Run.meta; None — нечего записывать."""
    if prices is None:
        return None
    return {"input": prices[0], "output": prices[1]}


def prices_from_meta(meta: dict[str, object] | None) -> tuple[float, float] | None:
    """Восстановить цены из Run.meta; отсутствие или порча записи — None.

    Терпимость намеренная и симметрична `RunOverride.from_meta`: запись
    старого формата (до задачи 2) или ручная правка meta не должна ронять
    resume, только вернуть его к резолвингу цен через каталог заново.
    """
    raw = (meta or {}).get(PRICES_META_KEY)
    if not isinstance(raw, dict):
        return None
    input_price, output_price = raw.get("input"), raw.get("output")
    if isinstance(input_price, bool) or isinstance(output_price, bool):
        return None
    if not isinstance(input_price, int | float) or not isinstance(output_price, int | float):
        return None
    return (float(input_price), float(output_price))


def run_meta_for(
    override: RunOverride, prices: tuple[float, float] | None
) -> dict[str, object] | None:
    """Meta run'а на старте: override и разрешённые им цены (задача 2).

    Пустой override — производной конфигурации нет вовсе, значит и цен нет
    (их резолвинг зависит от override.model): meta не нужна, как и раньше.
    """
    if override.is_empty():
        return None
    meta: dict[str, object] = {OVERRIDE_META_KEY: override.to_meta()}
    priced = prices_to_meta(prices)
    if priced is not None:
        meta[PRICES_META_KEY] = priced
    return meta


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

    # Sandbox — свойство сообщения, как и исполнитель. Эффективное значение
    # участвует в external-гейте ниже: override docker при конфиге
    # local-trusted легализует внешний агент для этого сообщения, и наоборот.
    effective_sandbox = ov.sandbox if ov.sandbox is not None else cfg.sandbox.type
    if ov.sandbox is not None and ov.sandbox != cfg.sandbox.type:
        update["sandbox"] = cfg.sandbox.model_copy(update={"type": ov.sandbox})

    if ov.adapter is not None:
        if ov.adapter not in EXTERNAL_ADAPTERS:
            raise OverrideError(
                f"неизвестный адаптер '{ov.adapter}'; известны: {', '.join(EXTERNAL_ADAPTERS)}"
            )
        kind = ov.executor if ov.executor is not None else cfg.executor.type
        if kind != "external":
            raise OverrideError(
                f"адаптер '{ov.adapter}' имеет смысл только с внешним агентом; "
                f"сейчас исполнитель native"
            )
        if cfg.executor.external is None:
            raise OverrideError(
                "внешний агент требует секцию executor.external в svarog.yaml "
                "(адаптер и образ sandbox, ADR-0016)"
            )
        update_external: dict[str, object] = {"adapter": ov.adapter}
        current_image = cfg.executor.external.image
        if current_image in _ADAPTER_IMAGES.values():
            wanted = _ADAPTER_IMAGES.get(ov.adapter)
            if wanted is None:
                raise OverrideError(
                    f"под адаптер '{ov.adapter}' в проекте нет готового образа: "
                    f"соберите свой и укажите его в executor.external.image — "
                    f"иначе запуск пойдёт в контейнер другого агента"
                )
            update_external["image"] = wanted
        if ov.adapter in _OPENAI_WIRE_ADAPTERS:
            # Секция, заточенная под claude-code (subscription/anthropic-URL),
            # для openai-адаптера непригодна: раньше model_copy молча
            # пропускал её мимо валидатора, opencode получал пустой ключ и
            # падал «Unauthorized: bridge» (31.07.2026). Провайдера собираем
            # из выбранной в композере карточки моделей — ровно того, что
            # человек и выбрал рядом с адаптером.
            current = cfg.executor.external
            unusable = current.auth != "api-key" or current.base_url == "https://api.anthropic.com"
            if unusable:
                provider_name = ov.provider if ov.provider is not None else cfg.models.default
                provider = cfg.models.providers.get(provider_name)
                if provider is None:
                    raise OverrideError(
                        f"адаптер '{ov.adapter}' требует OpenAI-совместимого "
                        f"провайдера, а секция executor.external настроена под "
                        f"claude-code и провайдер '{provider_name}' в "
                        f"models.providers не найден"
                    )
                base = provider.base_url.rstrip("/")
                base = base.removesuffix("/v1")
                update_external.update(
                    {
                        "auth": "api-key",
                        "api_key_ref": provider.api_key_ref,
                        "oauth_token_ref": None,
                        "base_url": base,
                        "model": ov.model if ov.model is not None else provider.model,
                    }
                )
        if ov.model is not None and "model" not in update_external:
            # Модель из композера доезжает до executor'а любым адаптером:
            # opencode — managed-конфиг, claude-code — --model, codex — -m.
            update_external["model"] = ov.model
        external = cfg.executor.external.model_copy(update=update_external)
        try:
            # model_copy(update=...) обходит валидаторы секции: несовместимая
            # комбинация обязана стать понятным 422 здесь, а не упавшим
            # контейнером в рантайме.
            external = ExternalExecutorConfig.model_validate(external.model_dump())
        except ValidationError as exc:
            first = exc.errors()[0].get("msg", str(exc)) if exc.errors() else str(exc)
            raise OverrideError(
                f"адаптер '{ov.adapter}' несовместим с executor.external: {first}"
            ) from exc
        update["executor"] = cfg.executor.model_copy(
            update={"type": "external", "external": external}
        )

    if ov.executor == "external" and cfg.executor.external is None:
        raise OverrideError(
            "внешний агент требует секцию executor.external в svarog.yaml "
            "(адаптер и образ sandbox, ADR-0016)"
        )
    # Гейт ADR-0016 по ЭФФЕКТИВНОЙ паре executor×sandbox: ловит и «external
    # override при local-trusted конфиге», и «sandbox=local-trusted override
    # при external конфиге» — внешний агент без контейнера не существует.
    effective_kind = ov.executor if ov.executor is not None else cfg.executor.type
    if ov.adapter is not None:
        effective_kind = "external"
    if effective_kind == "external" and effective_sandbox != "docker":
        raise OverrideError(
            f"внешний агент требует sandbox.type='docker', сейчас "
            f"'{effective_sandbox}' (fail-closed, ADR-0016): выберите sandbox "
            f"docker или исполнителя native"
        )
    # Ветка ov.executor ниже не должна затирать результат адаптера выше: если
    # adapter задан, executor уже учтён в update["executor"].
    if ov.executor is not None and ov.adapter is None:
        update["executor"] = cfg.executor.model_copy(update={"type": ov.executor})

    target = ov.provider if ov.provider is not None else cfg.models.default
    if ov.provider is not None and ov.provider not in cfg.models.providers:
        known = ", ".join(sorted(cfg.models.providers)) or "нет"
        raise OverrideError(
            f"провайдер '{ov.provider}' не описан в models.providers (есть: {known})"
        )

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
        update["models"] = cfg.models.model_copy(update={"default": target, "providers": providers})

    return cfg.model_copy(update=update)
