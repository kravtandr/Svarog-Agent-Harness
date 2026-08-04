"""Автогенерация названия чата по первому обмену (спека 2026-08-04).

Один best-effort вызов aux-модели по образцу memory/autocapture.py: любой
сбой модели или её сборки -> None, решение о fallback принимает вызывающий
(GatewayService._autotitle_bg). Исключения наружу не выходят.
"""

import logging
from collections.abc import Callable
from typing import Any

from svarog_harness.llm.provider import ChatMessage, ModelProvider

logger = logging.getLogger(__name__)

# «Безымянные» названия: хардкод клиента (web/src/App.tsx) и серверный
# дефолт (GatewayService.create_session). Только такие чаты переименовываем.
DEFAULT_TITLES = frozenset({"", "Новый чат", "gateway-сессия"})

_TITLE_MAX = 200  # лимит колонки sessions.title; rename_session не режет, поэтому режем здесь
_FALLBACK_MAX = 60
_TASK_LIMIT = 2000
_ANSWER_LIMIT = 1000
_QUOTES = "\"'«»“”‘’`"

_SYSTEM = (
    "Придумай короткое название диалогу: 3-6 слов, на языке диалога, "
    "без кавычек, без точки в конце. Верни ТОЛЬКО название, без пояснений."
)


def needs_autotitle(title: str | None, meta: dict[str, Any] | None) -> bool:
    """Генерировать ли название: дефолтное имя и не было прошлой попытки.

    Любое значение флага autotitle окончательно (в т.ч. "fallback"):
    следующие run'ы генерацию не перезапускают.
    """
    if (meta or {}).get("autotitle"):
        return False
    return (title or "").strip() in DEFAULT_TITLES


def clean_title(raw: str) -> str | None:
    """Нормализовать ответ модели; пустота после чистки -> None."""
    text = " ".join(raw.split()).strip(_QUOTES).strip()
    text = text.rstrip(".").strip(_QUOTES).strip()
    return text[:_TITLE_MAX] if text else None


def fallback_title(task: str) -> str | None:
    """Эвристика без модели: начало первого сообщения по границе слова."""
    text = " ".join(task.split())
    if not text:
        return None
    if len(text) <= _FALLBACK_MAX:
        return text
    cut = text[:_FALLBACK_MAX]
    head, _, _ = cut.rpartition(" ")
    return (head or cut).rstrip() + "…"


async def generate_title(provider: ModelProvider, task: str, answer: str) -> str | None:
    """Один вызов aux-модели; любой сбой -> None (fallback у вызывающего).

    Run мог упасть без ответа — тогда блок «Ответ:» не добавляется,
    название строится по одному вопросу (спека, «Промпт и вход»).
    """
    body = f"Вопрос:\n{task[:_TASK_LIMIT]}"
    if answer.strip():
        body += f"\n\nОтвет:\n{answer[:_ANSWER_LIMIT]}"
    messages = [
        ChatMessage(role="system", content=_SYSTEM),
        ChatMessage(role="user", content=body),
    ]
    try:
        result = await provider.complete(messages, [])
    except Exception:
        logger.warning("автоназвание: вызов aux-модели не удался", exc_info=True)
        return None
    return clean_title(result.content)


async def title_for(
    provider_factory: Callable[[], ModelProvider], task: str, answer: str
) -> str | None:
    """Собрать провайдера и сгенерировать название; сбой сборки — тоже None.

    Сборка вынесена под отдельный except: aux-модель может быть не
    сконфигурирована (ApiKeyError, нет провайдера) — это штатный случай
    fallback'а, а не ошибка фичи.
    """
    try:
        provider = provider_factory()
    except Exception:
        logger.warning("автоназвание: aux-провайдер недоступен", exc_info=True)
        return None
    return await generate_title(provider, task, answer)
