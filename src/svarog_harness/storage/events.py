"""EventStream: доставка событий run'а внешним интерфейсам (§6.1, §10.4).

Runtime не знает про WebSocket/Telegram — он публикует события run'а в
EventStream, а gateway их разбирает. Базовая реализация — in-process
pub/sub на asyncio.Queue (одного процесса достаточно для MVP+1, ADR-0007);
Redis-backed EventStream для multi-process server-режимов подключается той
же абстракцией. Источник истины по trace — SQLite; события — «живой» слой
для стриминга, поэтому потеря подписчика не теряет данные.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

# Терминальные типы событий: подписчик после них выходит из стрима (run
# завершён/приостановлен/ждёт approval — дальше поводов ждать в этом стриме нет).
_TERMINAL_TYPES = frozenset({"run_finished"})


class EventStream(ABC):
    @abstractmethod
    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        """Опубликовать событие run'а всем подписчикам и в историю.

        Синхронно — чтобы порядок событий (текстовые дельты, tool calls,
        финал run'а) сохранялся при вызове из sync-хуков runtime без гонок
        планировщика; put в очередь подписчика не блокирует.
        """

    @abstractmethod
    def reset(self, run_id: str) -> None:
        """Очистить историю run'а: при resume новая «нога» стримит с чистого листа.

        Полная история run'а всё равно доступна в trace (SQLite); event-stream —
        живой tail текущей ноги, поэтому старый терминальный `run_finished` не
        должен обрывать подписчика, подключившегося после возобновления.
        """

    @abstractmethod
    def stream(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        """Асинхронный итератор событий run'а: сначала история, затем живые."""


class InProcessEventStream(EventStream):
    """Pub/sub в пределах процесса; хранит ограниченную историю для реплея."""

    def __init__(self, history_limit: int = 2000) -> None:
        self._history_limit = history_limit
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}

    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        history = self._history.setdefault(run_id, [])
        history.append(event)
        if len(history) > self._history_limit:
            del history[: len(history) - self._history_limit]
        for queue in list(self._subscribers.get(run_id, [])):
            queue.put_nowait(event)

    def reset(self, run_id: str) -> None:
        self._history.pop(run_id, None)

    async def stream(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        # Регистрация подписчика и снимок истории — без await между ними,
        # поэтому событие не проскочит «между» реплеем и живой подпиской.
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        backlog = list(self._history.get(run_id, []))
        self._subscribers.setdefault(run_id, []).append(queue)
        try:
            for event in backlog:
                yield event
                if event.get("type") in _TERMINAL_TYPES:
                    return
            while True:
                event = await queue.get()
                yield event
                if event.get("type") in _TERMINAL_TYPES:
                    return
        finally:
            subs = self._subscribers.get(run_id)
            if subs is not None and queue in subs:
                subs.remove(queue)
