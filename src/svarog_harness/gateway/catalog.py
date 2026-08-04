"""Список моделей провайдера по openai-совместимому `/models`.

Модуль без состояния: кэш и резолвинг секретов живут в сервисе. URL
собирается как `{base_url}/models` — ровно то, что сделал бы openai-SDK со
своим `base_url`. Побочная польза: `base_url` без `/v1` даёт видимую
ошибку здесь, а не загадочное молчание при запуске.
"""

import math
from dataclasses import dataclass

import httpx

from svarog_harness.config.schema import ProviderConfig

# Список для выпадающего меню: 120 секунд из provider.timeout_sec — это
# зависший интерфейс.
CATALOG_TIMEOUT_SEC = 10.0


class CatalogError(Exception):
    """Провайдер не отдал список моделей; наружу уходит как HTTP 502."""


@dataclass(frozen=True)
class ModelCard:
    id: str
    name: str | None = None
    context_length: int | None = None
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None


def _price(raw: object) -> float | None:
    """USD за токен (OpenRouter отдаёт строкой) → USD за миллион токенов.

    `None` для отрицательных и не-конечных (NaN/inf) значений: цена уходит
    в `ProviderConfig.model_copy(update=...)` в обход `ge=0` схемы (v2 не
    ревалидирует `model_copy`), а отрицательная цена уменьшает `cost_usd`
    на каждом вызове — потолок `max_cost_usd_per_run` молча перестаёт
    срабатывать (runtime/loop.py). OpenRouter отдаёт "-1" для
    router pseudo-моделей вроде `openrouter/auto`.

    `bool` исключён явно: это подкласс `int` в Python, и `"prompt": true`
    без проверки стал бы $1,000,000/Mtok.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str | int | float):
        try:
            value = float(raw)
        except ValueError:
            return None
        if not math.isfinite(value) or value < 0:
            return None
        return value * 1_000_000
    return None


def parse_models(payload: dict[str, object]) -> list[ModelCard]:
    """Терпимый разбор: чего нет — None, что не разбирается — пропускаем.

    Форматы разные: у OpenRouter есть name/context_length/pricing, у голого
    OpenAI — только id. Ронять весь список из-за одной кривой записи нельзя.
    """
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    cards: list[ModelCard] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        name = item.get("name")
        context = item.get("context_length")
        pricing = item.get("pricing")
        pricing = pricing if isinstance(pricing, dict) else {}
        cards.append(
            ModelCard(
                id=model_id,
                name=name if isinstance(name, str) else None,
                context_length=context if isinstance(context, int) else None,
                input_usd_per_mtok=_price(pricing.get("prompt")),
                output_usd_per_mtok=_price(pricing.get("completion")),
            )
        )
    return cards


async def fetch_models(
    provider: ProviderConfig,
    api_key: str | None,
    *,
    timeout: float = CATALOG_TIMEOUT_SEC,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[ModelCard]:
    """Список моделей провайдера. Ключ уходит только в заголовок запроса.

    base_url без `/v1` — частая ошибка конфига (OpenRouter отдаёт HTML на
    /api/models, найдено 31.07.2026): после провала пробуем `{base}/v1/models`
    и, если там живой каталог, отвечаем им — вместо загадочного «ответ не
    JSON». Ошибка первого URL сохраняется в тексте, чтобы конфиг всё же
    поправили.
    """
    base = provider.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    urls = [f"{base}/models"]
    if not base.endswith("/v1"):
        urls.append(f"{base}/v1/models")
    last_error = ""
    # LM Studio отвечает 200 валидным JSON на любой путь («Unexpected
    # endpoint... Returning 200 anyway»), поэтому 200 без списка моделей —
    # повод попробовать следующий кандидат, а не успех. Пустоту запоминаем:
    # если /v1 тоже ничего не дал, честно пустой каталог лучше ошибки.
    empty: list[ModelCard] | None = None
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for url in urls:
            try:
                response = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"{url}: {exc}"
                continue
            if response.status_code >= 400:
                last_error = f"{url}: провайдер ответил {response.status_code}"
                continue
            try:
                payload = response.json()
            except ValueError:
                last_error = f"{url}: ответ не JSON (base_url без /v1?)"
                continue
            cards = parse_models(payload if isinstance(payload, dict) else {})
            if cards:
                return cards
            empty = cards
    if empty is not None:
        return empty
    raise CatalogError(last_error or f"{base}: список моделей недоступен")
