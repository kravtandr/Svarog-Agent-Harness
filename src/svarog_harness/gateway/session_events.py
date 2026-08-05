"""SessionEventHub: fan-out событий сессий для WS /sessions/events (спека 2026-08-05).

Без истории: подписчик получает события с момента подписки, начальное
состояние клиент берёт из GET /sessions. Очередь подписчика ограничена;
переполнение молча дропает событие — события косметические (названия
чатов), истина всегда в БД.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

_QUEUE_LIMIT = 100


class SessionEventHub:
    """Простой fan-out: publish синхронный, подписчики — async-генераторы."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def publish(self, event: dict[str, Any]) -> None:
        for queue in self._subscribers:
            # Отстающий подписчик теряет событие, а не копит бэклог:
            # актуальные названия он возьмёт из GET /sessions.
            with suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)
