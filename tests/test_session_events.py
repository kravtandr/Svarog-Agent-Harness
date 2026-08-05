"""Юнит-тесты SessionEventHub: fan-out без истории (спека 2026-08-05)."""

import asyncio

from svarog_harness.gateway.session_events import SessionEventHub


async def test_subscriber_receives_published_events() -> None:
    hub = SessionEventHub()
    received: list[dict] = []

    async def consume() -> None:
        async for event in hub.subscribe():
            received.append(event)
            if len(received) == 2:
                break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # подписка зарегистрировалась (генератор дошёл до get)
    hub.publish({"n": 1})
    hub.publish({"n": 2})
    await asyncio.wait_for(task, 1)
    assert received == [{"n": 1}, {"n": 2}]


async def test_two_subscribers_both_receive() -> None:
    hub = SessionEventHub()
    first: list[dict] = []
    second: list[dict] = []

    async def consume(sink: list[dict]) -> None:
        async for event in hub.subscribe():
            sink.append(event)
            break

    tasks = [
        asyncio.create_task(consume(first)),
        asyncio.create_task(consume(second)),
    ]
    await asyncio.sleep(0)
    hub.publish({"n": 1})
    await asyncio.wait_for(asyncio.gather(*tasks), 1)
    assert first == [{"n": 1}] and second == [{"n": 1}]


async def test_overflow_drops_extra_events_silently() -> None:
    hub = SessionEventHub()
    agen = hub.subscribe()
    waiting = asyncio.create_task(anext(agen))
    await asyncio.sleep(0)  # get() уже ждёт
    hub.publish({"n": 0})  # уходит ждущему get напрямую
    assert await asyncio.wait_for(waiting, 1) == {"n": 0}
    for i in range(1, 202):
        hub.publish({"n": i})  # 201 событие в очередь лимита 100 — без исключений
    seen = [await asyncio.wait_for(anext(agen), 1) for _ in range(100)]
    assert seen[0] == {"n": 1}
    assert seen[-1] == {"n": 100}  # 101..201 молча дропнуты
    await agen.aclose()


async def test_closed_subscription_does_not_break_publish() -> None:
    hub = SessionEventHub()
    agen = hub.subscribe()
    waiting = asyncio.create_task(anext(agen))
    await asyncio.sleep(0)
    hub.publish({"n": 0})
    await asyncio.wait_for(waiting, 1)
    await agen.aclose()
    hub.publish({"n": 1})  # подписчиков нет — не исключение
