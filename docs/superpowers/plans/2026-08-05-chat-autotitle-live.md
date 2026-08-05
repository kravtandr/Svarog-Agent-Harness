# Живое автоназвание чатов — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Черновик названия чата сразу после первого сообщения, уточнение после ответа, пуш обоих обновлений в UI по WebSocket и посимвольная анимация в сайдбаре и топ-баре.

**Architecture:** Новый `SessionEventHub` (fan-out без истории) живёт полем `GatewayService`; WorkspaceHub делит один hub на все корни, TenantHub оставляет per-tenant дефолт. Двухфазная генерация: `_autotitle_draft_bg` из `send_message` (по вопросу) и переработанный `_autotitle_bg` после run'а (по вопросу+ответу), обе фазы публикуют `session_title`. Фронтенд держит постоянный WS `/sessions/events` с реконнектом и анимирует смену названия компонентом `AnimatedTitle`.

**Tech Stack:** Python 3.12 (asyncio, SQLAlchemy async, FastAPI WS), React + TypeScript (vitest, fake timers).

**Спека:** `docs/superpowers/specs/2026-08-05-chat-autotitle-live-design.md`

## Global Constraints

- Комментарии и docstrings — по-русски, в стиле кодовой базы.
- Перед каждым коммитом бэкенда: `uv run ruff check src tests && uv run ruff format src tests`, `uv run mypy <изменённые файлы>` (в `service.py` есть ОДНА pre-existing ошибка `live_run_on_workspace` — не чинить, новых не добавлять), целевые тесты зелёные. Фронтенд: `npm --prefix web test` (tsc + prettier + vitest).
- Дефолтные названия ровно: `""`, `"Новый чат"`, `"gateway-сессия"`.
- Флаги: `autotitle: "draft"` + `autotitle_draft: <title>` — черновик; `"done"`/`"fallback"` — окончательно, ничего не перезапускается.
- Событие: `{"type": "session_title", "session_id": ..., "title": ..., "phase": "draft" | "final"}`; `final` публикуется только если название реально изменилось.
- Ошибки генерации — best-effort: warning в лог, ничего не роняют.
- Очередь подписчика hub'а — 100 событий, переполнение молча дропает.
- В multi-tenant hub строго per-tenant (это дефолт `default_factory` — в TenantHub ничего не менять и не передавать).
- Если рабочая копия свежая: `uv python pin 3.12 && uv sync` перед первым запуском тестов (без пина uv берёт 3.14, на котором не ставится onnxruntime).

---

### Task 1: `SessionEventHub` — fan-out событий сессий

**Files:**
- Create: `src/svarog_harness/gateway/session_events.py`
- Test: `tests/test_session_events.py`

**Interfaces:**
- Consumes: только stdlib (`asyncio`).
- Produces (Task 3 полагается на эти сигнатуры): класс `SessionEventHub` с методами `publish(event: dict[str, Any]) -> None` (синхронный) и `subscribe() -> AsyncIterator[dict[str, Any]]` (async-генератор; отписка — завершение/закрытие генератора).

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_session_events.py`:

```python
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
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_session_events.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.gateway.session_events'`

- [ ] **Step 3: Реализовать модуль**

Создать `src/svarog_harness/gateway/session_events.py`:

```python
"""SessionEventHub: fan-out событий сессий для WS /sessions/events (спека 2026-08-05).

Без истории: подписчик получает события с момента подписки, начальное
состояние клиент берёт из GET /sessions. Очередь подписчика ограничена;
переполнение молча дропает событие — события косметические (названия
чатов), истина всегда в БД.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

_QUEUE_LIMIT = 100


class SessionEventHub:
    """Простой fan-out: publish синхронный, подписчики — async-генераторы."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def publish(self, event: dict[str, Any]) -> None:
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Отстающий подписчик теряет событие, а не копит бэклог:
                # актуальные названия он возьмёт из GET /sessions.
                pass

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest tests/test_session_events.py -v`
Expected: PASS (4 теста)

- [ ] **Step 5: Линт, типы, коммит**

```bash
uv run ruff check src/svarog_harness/gateway/session_events.py tests/test_session_events.py && uv run ruff format src/svarog_harness/gateway/session_events.py tests/test_session_events.py && uv run mypy src/svarog_harness/gateway/session_events.py
git add src/svarog_harness/gateway/session_events.py tests/test_session_events.py
git commit -m "feat(gateway): SessionEventHub — fan-out событий сессий без истории"
```

---

### Task 2: Двухфазные условия в `autotitle.py`

**Files:**
- Modify: `src/svarog_harness/gateway/autotitle.py` (заменить `needs_autotitle` на `needs_draft`/`needs_refine`; остальные функции не трогать)
- Modify: `tests/test_autotitle.py` (заменить тест `test_needs_autotitle_only_for_default_titles_without_flag`)

**Interfaces:**
- Consumes: существующие `DEFAULT_TITLES`, `clean_title`, `fallback_title`, `generate_title`, `title_for` — без изменений.
- Produces (Task 3 полагается на сигнатуры):
  - `needs_draft(title: str | None, meta: dict[str, Any] | None) -> bool`
  - `needs_refine(title: str | None, meta: dict[str, Any] | None) -> bool`
  - `needs_autotitle` УДАЛЯЕТСЯ (единственный потребитель — `service._autotitle_bg` — переводится на новые функции в Task 3; Task 2 и Task 3 коммитятся подряд, между ними полный набор не гоняется — упадёт только импорт в service.py, поэтому в Task 2 сразу поправить импорт в `service.py`: `needs_autotitle` → `needs_refine`, и в `_autotitle_bg` заменить оба вызова `needs_autotitle(` на `needs_refine(` — поведенчески для старых сессий это эквивалентно, см. Step 3).

- [ ] **Step 1: Написать падающие тесты**

В `tests/test_autotitle.py` удалить `test_needs_autotitle_only_for_default_titles_without_flag`, убрать `needs_autotitle` из импорта, добавить в импорт `needs_draft, needs_refine` и тесты:

```python
def test_needs_draft_only_for_default_titles_without_flag() -> None:
    assert needs_draft("Новый чат", None)
    assert needs_draft("gateway-сессия", {})
    assert needs_draft("", {})
    assert needs_draft(None, {})
    assert not needs_draft("Мой чат", {})
    assert not needs_draft("Новый чат", {"autotitle": "draft"})
    assert not needs_draft("Новый чат", {"autotitle": "done"})


def test_needs_refine_after_draft() -> None:
    meta = {"autotitle": "draft", "autotitle_draft": "Черновик"}
    assert needs_refine("Черновик", meta)
    # Черновик переименовали вручную (CLI) — не перетираем.
    assert not needs_refine("Моё имя", meta)


def test_needs_refine_without_draft_uses_default_condition() -> None:
    assert needs_refine("Новый чат", {})
    assert needs_refine("gateway-сессия", None)
    assert not needs_refine("Мой чат", {})


def test_needs_refine_final_flags_block() -> None:
    assert not needs_refine("Название", {"autotitle": "done"})
    assert not needs_refine("Название", {"autotitle": "fallback"})
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_autotitle.py -v`
Expected: FAIL — `ImportError: cannot import name 'needs_draft'`

- [ ] **Step 3: Реализация**

В `src/svarog_harness/gateway/autotitle.py` заменить функцию `needs_autotitle` (строки 32-40) на:

```python
def needs_draft(title: str | None, meta: dict[str, Any] | None) -> bool:
    """Фаза черновика (спека 2026-08-05): дефолтное имя и попыток не было."""
    if (meta or {}).get("autotitle"):
        return False
    return (title or "").strip() in DEFAULT_TITLES


def needs_refine(title: str | None, meta: dict[str, Any] | None) -> bool:
    """Фаза уточнения после ответа (спека 2026-08-05).

    Черновик уточняем, только если его не переименовали вручную (сверка с
    autotitle_draft). Без черновика — старое условие (дефолтное имя без
    флага): путь для сессий, где фаза черновика не случилась. done/fallback
    окончательны.
    """
    flag = (meta or {}).get("autotitle")
    if flag == "draft":
        return (title or "") == (meta or {}).get("autotitle_draft")
    if flag:
        return False
    return (title or "").strip() in DEFAULT_TITLES
```

Обновить шапку модуля: первая строка docstring → `"""Автогенерация названия чата: черновик по вопросу, уточнение по ответу (спеки 2026-08-04, 2026-08-05).`

В `src/svarog_harness/gateway/service.py` (только чтобы не сломать импорт до Task 3): в импорте из `svarog_harness.gateway.autotitle` заменить `needs_autotitle` на `needs_refine`, в `_autotitle_bg` заменить оба вызова `needs_autotitle(` на `needs_refine(`. Для сессий без черновика `needs_refine` эквивалентен старому `needs_autotitle`, поэтому существующие тесты `tests/test_gateway_autotitle.py` остаются зелёными.

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest tests/test_autotitle.py tests/test_gateway_autotitle.py -q`
Expected: PASS

- [ ] **Step 5: Линт, типы, коммит**

```bash
uv run ruff check src/svarog_harness/gateway tests/test_autotitle.py && uv run ruff format src/svarog_harness/gateway tests/test_autotitle.py && uv run mypy src/svarog_harness/gateway/autotitle.py
git add src/svarog_harness/gateway/autotitle.py src/svarog_harness/gateway/service.py tests/test_autotitle.py
git commit -m "feat(gateway): условия двух фаз автоназвания — needs_draft/needs_refine"
```

---

### Task 3: Две фазы в сервисе, публикация событий, WS-эндпоинт, hub-wiring

**Files:**
- Modify: `src/svarog_harness/gateway/service.py` (импорты; поле `session_events` после `events` ~строка 216; спавн черновика в `send_message` перед `return await started`; новый `_autotitle_draft_bg`; переработка `_autotitle_bg` ~строки 780-860)
- Modify: `src/svarog_harness/gateway/api.py` (новый `@app.websocket("/sessions/events")` рядом с `run_events` ~строка 611)
- Modify: `src/svarog_harness/gateway/hub.py` (общий hub в `WorkspaceHub`)
- Test: `tests/test_gateway_autotitle.py` (переработать: две фазы + события), плюс два WS-теста туда же

**Interfaces:**
- Consumes из Task 1: `SessionEventHub.publish(dict)`, `.subscribe()`.
- Consumes из Task 2: `needs_draft`, `needs_refine` (плюс существующие `fallback_title`, `title_for`).
- Produces: поле `GatewayService.session_events: SessionEventHub`; методы `_autotitle_draft_bg(session_id: str, task_text: str)`, `_autotitle_bg(run_id: str, answer: str)` (сигнатура не меняется); WS `/sessions/events` (auth как у `/runs/{id}/events`, 1008 до accept). Task 4 полагается на формат события из Global Constraints.

- [ ] **Step 1: Переписать тесты `tests/test_gateway_autotitle.py`**

Файл сохраняет фикстуру `service`, `_write_config`, `ScriptedProvider`, `_patch_agent`, `_final`, `_session_state` как есть. Заменить `TitleProvider` и все тесты на:

```python
class TitleProvider(ModelProvider):
    """Aux-модель названий: скриптованные ответы по фазам, пишет промпты."""

    def __init__(self, titles: list[str], *, error: bool = False) -> None:
        self.titles = list(titles)
        self.error = error
        self.prompts: list[str] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.prompts.append(messages[-1].content)
        if self.error:
            raise RuntimeError("aux недоступна")
        return CompletionResult(content=self.titles.pop(0), usage=Usage(1, 1))


def _spy_events(service: GatewayService) -> list[dict[str, Any]]:
    """Собрать публикуемые события: publish-шпион поверх настоящего hub'а."""
    events: list[dict[str, Any]] = []
    original = service.session_events.publish

    def spy(event: dict[str, Any]) -> None:
        events.append(event)
        original(event)

    service.session_events.publish = spy  # type: ignore[method-assign]
    return events


async def test_draft_then_refine_with_events(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("Париж — столица Франции")])
    aux = TitleProvider(["Черновик названия", "Финальное название"])
    _patch_title(monkeypatch, aux)
    events = _spy_events(service)

    view = await service.create_session(title="Новый чат")
    await service.send_message(view.session_id, "Какая столица Франции?", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "Финальное название"
    assert meta["autotitle"] == "done"
    assert meta["autotitle_draft"] == "Черновик названия"
    # Черновик — по одному вопросу, уточнение — с ответом.
    assert "Ответ:" not in aux.prompts[0]
    assert "Ответ:" in aux.prompts[1]
    assert [e["phase"] for e in events] == ["draft", "final"]
    assert events[0]["title"] == "Черновик названия"
    assert events[1]["title"] == "Финальное название"
    assert all(e["type"] == "session_title" and e["session_id"] == view.session_id for e in events)


async def test_refine_equal_to_draft_publishes_once(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("ответ")])
    aux = TitleProvider(["Одно название", "Одно название"])
    _patch_title(monkeypatch, aux)
    events = _spy_events(service)

    view = await service.create_session(title="Новый чат")
    await service.send_message(view.session_id, "вопрос", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "Одно название"
    assert meta["autotitle"] == "done"
    assert [e["phase"] for e in events] == ["draft"]


async def test_aux_error_falls_back_to_truncated_question(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("ответ")])
    aux = TitleProvider([], error=True)
    _patch_title(monkeypatch, aux)
    events = _spy_events(service)

    view = await service.create_session(title="Новый чат")
    await service.send_message(view.session_id, "Какая столица Франции?", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    # Черновик = fallback-обрезка; уточнение тоже упало -> черновик остаётся.
    assert title == "Какая столица Франции?"
    assert meta["autotitle"] == "done"
    assert [e["phase"] for e in events] == ["draft"]


async def test_manually_renamed_draft_not_overwritten(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("ответ")])
    aux = TitleProvider(["Не должно применяться"])
    _patch_title(monkeypatch, aux)

    view = await service.create_session(title="Новый чат")

    async def rename(db: Any) -> None:
        row = await db.get(Session, view.session_id)
        row.title = "Ручное имя"
        row.meta = {**(row.meta or {}), "autotitle": "draft", "autotitle_draft": "Черновик"}
        await db.commit()

    await service._read(rename)
    events = _spy_events(service)
    await service.send_message(view.session_id, "вопрос", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "Ручное имя"
    assert meta["autotitle"] == "draft"  # уточнение не сработало и флаг не тронут
    assert events == []
    assert aux.prompts == []  # ни одна фаза не звала модель


async def test_custom_title_untouched(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("ответ")])
    aux = TitleProvider(["Не должно применяться"])
    _patch_title(monkeypatch, aux)
    events = _spy_events(service)

    view = await service.create_session(title="Мой проект")
    await service.send_message(view.session_id, "вопрос", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "Мой проект"
    assert "autotitle" not in meta
    assert events == []
    assert aux.prompts == []


def test_session_events_ws_auth(service: GatewayService) -> None:
    from fastapi.testclient import TestClient

    from svarog_harness.gateway.api import create_app

    client = TestClient(create_app(service, bearer_token="secret-token"))
    try:
        with client.websocket_connect("/sessions/events"):
            pass
        raise AssertionError("без токена соединение должно быть отклонено")
    except AssertionError:
        raise
    except Exception:
        pass  # 1008 policy violation — ожидаемо
    with client.websocket_connect("/sessions/events?token=secret-token") as ws:
        # Успешный handshake подтверждает auth-путь; доставка событий покрыта
        # publish-шпионами выше (TestClient обрывает fire-and-forget задачи).
        ws.close()


def test_workspace_hub_shares_session_events(tmp_path: Path) -> None:
    from svarog_harness.gateway.hub import WorkspaceHub
    from svarog_harness.gateway.roots import WorkspaceRootsRegistry

    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    _write_config(root_a, tmp_path)
    hub = WorkspaceHub(
        base_cfg=load_config(project_dir=root_a),
        default_root=root_a,
        registry=WorkspaceRootsRegistry(tmp_path / "roots.json"),
    )
    svc_b = hub.service_for(root_b)
    assert svc_b.session_events is hub.service_for(root_a).session_events
```

Также добавить недостающие импорты в шапку файла: `from typing import Any`, `from svarog_harness.storage.models import Session` (уже есть), `from pathlib import Path` (уже есть). Хелпер `_patch_title` остаётся прежним (подмена `service_module.auxiliary_provider`).

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_gateway_autotitle.py -q`
Expected: FAIL — `AttributeError: 'GatewayService' object has no attribute 'session_events'` (и/или несуществующий `_autotitle_draft_bg`)

- [ ] **Step 3: Реализация в service.py**

3a. Импорты: в импорт из `svarog_harness.gateway.autotitle` добавить `needs_draft` (итог: `fallback_title, needs_draft, needs_refine, title_for`); добавить `from svarog_harness.gateway.session_events import SessionEventHub`.

3b. Поле dataclass — после `events: EventStream = field(default_factory=InProcessEventStream)`:

```python
    # Канал событий сессий для WS /sessions/events (спека 2026-08-05):
    # WorkspaceHub передаёт общий hub всем корням, TenantHub оставляет
    # per-tenant дефолт — жильцы не видят чужих названий.
    session_events: SessionEventHub = field(default_factory=SessionEventHub)
```

3c. В `send_message` перед `return await started`:

```python
        # Черновик названия по одному вопросу (спека 2026-08-05): не ждёт
        # ни старта run'а, ни его завершения.
        self._spawn(self._autotitle_draft_bg(session.id, text))
```

3d. Новый метод (перед `_autotitle_bg`):

```python
    async def _autotitle_draft_bg(self, session_id: str, task_text: str) -> None:
        """Черновик названия по одному вопросу (спека 2026-08-05): best-effort.

        Сбой модели -> fallback-обрезка вопроса: что-то осмысленное появляется
        в сайдбаре сразу, а уточнение после ответа всё равно попробует лучше.
        """
        try:
            if not task_text.strip():
                return

            async def read(db: AsyncSession) -> bool:
                session = await db.get(Session, session_id)
                return session is not None and needs_draft(session.title, session.meta)

            if not await self._read(read):
                return
            generated = await title_for(
                lambda: auxiliary_provider(
                    self.cfg.models, default_secret_store(self.cfg.secrets.path)
                ),
                task_text,
                "",
            )
            draft = generated or fallback_title(task_text)
            if draft is None:
                return
            draft_title: str = draft

            async def write(db: AsyncSession) -> bool:
                session = await db.get(Session, session_id)
                if session is None or not needs_draft(session.title, session.meta):
                    return False  # гонка: быстрое уточнение успело раньше
                session.title = draft_title
                # JSON-колонка без MutableDict: только присваивание нового dict.
                session.meta = {
                    **(session.meta or {}),
                    "autotitle": "draft",
                    "autotitle_draft": draft_title,
                }
                await db.commit()
                return True

            if await self._read(write):
                self.session_events.publish(
                    {
                        "type": "session_title",
                        "session_id": session_id,
                        "title": draft_title,
                        "phase": "draft",
                    }
                )
        except Exception:
            logger.warning("автоназвание: черновик не удался", exc_info=True)
            return
```

3e. Переписать `_autotitle_bg` целиком:

```python
    async def _autotitle_bg(self, run_id: str, answer: str) -> None:
        """Уточнение названия чата после ответа (спека 2026-08-05): best-effort.

        Отдельная фоновая задача после run_finished: сбой модели или БД не
        влияет на run. done/fallback окончательны; черновик, переименованный
        вручную (CLI), не перетирается — это решает needs_refine.
        """
        try:

            async def read(db: AsyncSession) -> tuple[str, str, str, bool] | None:
                run = await db.get(Run, run_id)
                if run is None:
                    return None
                session = await db.get(Session, run.session_id)
                if session is None or not needs_refine(session.title, session.meta):
                    return None
                first = (
                    await db.execute(
                        select(Run.task)
                        .where(Run.session_id == session.id)
                        .order_by(Run.created_at, Run.id)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                had_draft = (session.meta or {}).get("autotitle") == "draft"
                return session.id, first or "", session.title or "", had_draft

            found = await self._read(read)
            if found is None:
                return
            session_id, first_task, current_title, had_draft = found
            if not first_task.strip():
                return
            generated = await title_for(
                lambda: auxiliary_provider(
                    self.cfg.models, default_secret_store(self.cfg.secrets.path)
                ),
                first_task,
                answer,
            )
            if generated is not None:
                final_title, flag = generated, "done"
            elif had_draft:
                # Модель упала, но черновик уже стоит: он лучше обрезки.
                final_title, flag = current_title, "done"
            else:
                fb = fallback_title(first_task)
                if fb is None:
                    return
                final_title, flag = fb, "fallback"

            async def write(db: AsyncSession) -> bool:
                session = await db.get(Session, session_id)
                if session is None or not needs_refine(session.title, session.meta):
                    return False  # гонка: параллельный run уже уточнил
                changed = (session.title or "") != final_title
                session.title = final_title
                # JSON-колонка без MutableDict: только присваивание нового dict.
                session.meta = {**(session.meta or {}), "autotitle": flag}
                await db.commit()
                return changed

            # _read — обёртка with_db и годится и для записи (историческое имя).
            if await self._read(write):
                self.session_events.publish(
                    {
                        "type": "session_title",
                        "session_id": session_id,
                        "title": final_title,
                        "phase": "final",
                    }
                )
        except Exception:
            # Автоназвание никогда не роняет фоновую задачу (best-effort, спека).
            logger.warning("автоназвание: фоновая задача не удалась", exc_info=True)
            return
```

- [ ] **Step 4: WS-эндпоинт в api.py**

После обработчика `run_events` (за `await websocket.close()`, ~строка 631) добавить:

```python
    @app.websocket("/sessions/events")
    async def session_events(websocket: WebSocket) -> None:
        """Пуш обновлений названий чатов (спека 2026-08-05).

        Канал без истории: начальное состояние клиент берёт из GET /sessions.
        Маршрутизация по id не нужна: в WorkspaceHub hub общий на все корни,
        в multi-tenant authenticate уже вернул сервис тенанта с его hub'ом.
        """
        query_token = websocket.query_params.get("token")
        authorization = websocket.headers.get("authorization")
        service = resolver.authenticate(authorization, query_token=query_token)
        if service is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await websocket.accept()
        try:
            async for event in service.session_events.subscribe():
                await websocket.send_json(event)
        except WebSocketDisconnect:
            return
```

- [ ] **Step 5: Общий hub в WorkspaceHub (hub.py)**

Импорт: `from svarog_harness.gateway.session_events import SessionEventHub`.

В dataclass `WorkspaceHub` после `_services: dict[...] = field(...)` добавить поле:

```python
    # Один канал событий сессий на все корни (спека 2026-08-05): Nav
    # показывает сессии всех корней — и события должны приходить все.
    session_events: SessionEventHub = field(default_factory=SessionEventHub, init=False)
```

В `_make_service` передать его сервису:

```python
        return GatewayService(
            cfg,
            root,
            session_events=self.session_events,
            on_run_created=lambda run_id: self.registry.record_run(run_id, root),
            on_session_created=lambda session_id: self.registry.record_session(session_id, root),
        )
```

`TenantHub` и `SingleTenantResolver` не трогать (per-tenant/одиночный hub — дефолт поля).

- [ ] **Step 6: Убедиться, что тесты проходят + регрессия**

Run: `uv run pytest tests/test_gateway_autotitle.py -v`
Expected: PASS (7 тестов)

Run: `uv run pytest tests/test_gateway.py tests/test_autotitle.py tests/test_session_events.py tests/test_workspace_hub.py tests/test_tenant_gateway.py -q`
Expected: PASS, без новых падений

- [ ] **Step 7: Линт, типы, коммит**

```bash
uv run ruff check src/svarog_harness/gateway tests && uv run ruff format src/svarog_harness/gateway tests && uv run mypy src/svarog_harness/gateway/service.py src/svarog_harness/gateway/api.py src/svarog_harness/gateway/hub.py
git add src/svarog_harness/gateway/service.py src/svarog_harness/gateway/api.py src/svarog_harness/gateway/hub.py tests/test_gateway_autotitle.py
git commit -m "feat(gateway): двухфазное автоназвание + WS /sessions/events"
```

---

### Task 4: Фронтенд — подписка, реконнект, анимация набора

**Files:**
- Modify: `web/src/api/types.ts` (тип `SessionEvent`)
- Modify: `web/src/api/stream.ts` (функция `subscribeSessionEvents`)
- Create: `web/src/components/AnimatedTitle.tsx`
- Modify: `web/src/components/Nav.tsx:214` (строка `<span className="nav__title">{session.title}</span>`)
- Modify: `web/src/App.tsx` (эффект подписки; топ-бар ~строка 177)
- Modify: `web/src/setupTests.ts` (глобальный no-op стаб WebSocket)
- Test: `web/src/components/AnimatedTitle.test.tsx`, дополнение `web/src/App.test.tsx`

**Interfaces:**
- Consumes: WS `/sessions/events` из Task 3, формат события из Global Constraints.
- Produces: `subscribeSessionEvents(baseUrl: string, token: string | undefined, onEvent: (e: SessionEvent) => void, onClose?: () => void): () => void`; компонент `AnimatedTitle({ text, className }: { text: string; className?: string })`.

- [ ] **Step 1: Написать падающие тесты**

Создать `web/src/components/AnimatedTitle.test.tsx`:

```tsx
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AnimatedTitle } from "./AnimatedTitle";

describe("AnimatedTitle", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("первый маунт рендерит текст сразу, без анимации", () => {
    render(<AnimatedTitle text="Готовое название" />);
    expect(screen.getByText("Готовое название")).toBeInTheDocument();
  });

  it("смена текста печатается посимвольно до конца", () => {
    vi.useFakeTimers();
    const { rerender } = render(<AnimatedTitle text="Старое" />);
    rerender(<AnimatedTitle text="Новое имя" />);
    // Спустя два тика напечатана только часть.
    act(() => {
      vi.advanceTimersByTime(2 * 25);
    });
    expect(screen.queryByText("Новое имя")).not.toBeInTheDocument();
    // Достаточно тиков — текст полный.
    act(() => {
      vi.advanceTimersByTime(25 * "Новое имя".length);
    });
    expect(screen.getByText("Новое имя")).toBeInTheDocument();
  });
});
```

В `web/src/App.test.tsx` добавить тест (в существующий `describe`; импорты `act` из `@testing-library/react` и `afterEach` при необходимости):

```tsx
  it("session_title из WS обновляет название в списке", async () => {
    class FakeSocket {
      static last: FakeSocket | null = null;
      onmessage: ((ev: { data: string }) => void) | null = null;
      onclose: (() => void) | null = null;
      constructor() {
        FakeSocket.last = this;
      }
      close() {}
    }
    vi.stubGlobal("WebSocket", FakeSocket);

    render(<App api={api()} />);
    await screen.findByRole("button", { name: "FTS-поиск по памяти" });

    act(() => {
      FakeSocket.last?.onmessage?.({
        data: JSON.stringify({
          type: "session_title",
          session_id: "s1",
          title: "Новое имя чата",
          phase: "draft",
        }),
      });
    });
    expect(
      await screen.findByRole("button", { name: /Новое имя чата/ }),
    ).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `npm --prefix web test`
Expected: FAIL — `AnimatedTitle` не существует; новый App-тест не находит обновлённое имя.

- [ ] **Step 3: Реализация**

3a. `web/src/api/types.ts` — добавить:

```typescript
/** Событие канала /sessions/events (спека 2026-08-05). */
export interface SessionEvent {
  type: string;
  session_id: string;
  title: string;
  phase: "draft" | "final";
}
```

3b. `web/src/api/stream.ts` — добавить (импорт: `import type { SessionEvent } from "./types";`):

```typescript
/**
 * Подписка на события сессий (названия чатов, спека 2026-08-05).
 *
 * Возвращает функцию отписки; onClose зовётся при закрытии сокета извне
 * (обрыв сети, рестарт сервера) — на нём клиент строит реконнект. Ручная
 * отписка onClose не зовёт.
 */
export function subscribeSessionEvents(
  baseUrl: string,
  token: string | undefined,
  onEvent: (event: SessionEvent) => void,
  onClose?: () => void,
): () => void {
  const base = baseUrl || window.location.origin;
  const url = new URL("/sessions/events", base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  if (token) url.searchParams.set("token", token);

  const socket = new WebSocket(url);
  socket.onmessage = (message: MessageEvent<string>) => {
    try {
      onEvent(JSON.parse(message.data) as SessionEvent);
    } catch {
      // Битое событие пропускаем: одна плохая строка не валит канал.
    }
  };
  if (onClose) socket.onclose = onClose;
  return () => {
    socket.onclose = null;
    socket.close();
  };
}
```

3c. Создать `web/src/components/AnimatedTitle.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";

/** Скорость печати: символ в 25 мс — заметно, но не тянет. */
const TICK_MS = 25;

/**
 * Название с анимацией набора текста (спека 2026-08-05).
 *
 * Первый маунт рендерит текст сразу: при загрузке списка чатов ничего
 * «печататься» не должно. Анимация — только на смену text (пуш нового
 * названия). prefers-reduced-motion отключает её совсем.
 */
export function AnimatedTitle({
  text,
  className,
}: {
  text: string;
  className?: string;
}) {
  const [shown, setShown] = useState(text);
  const prev = useRef(text);

  useEffect(() => {
    if (prev.current === text) return;
    prev.current = text;
    const reduce =
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    if (reduce) {
      setShown(text);
      return;
    }
    let i = 0;
    setShown("");
    const timer = window.setInterval(() => {
      i += 1;
      setShown(text.slice(0, i));
      if (i >= text.length) window.clearInterval(timer);
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, [text]);

  return <span className={className}>{shown}</span>;
}
```

3d. `web/src/components/Nav.tsx` — импорт `import { AnimatedTitle } from "./AnimatedTitle";`, строку 214 заменить:

```tsx
                        <AnimatedTitle
                          className="nav__title"
                          text={session.title}
                        />
```

3e. `web/src/App.tsx`:

Импорты: `import { subscribeSessionEvents } from "./api/stream";` и `import { AnimatedTitle } from "./components/AnimatedTitle";`.

Эффект после блока busy-поллинга (~строка 127):

```tsx
  // Живые названия чатов (спека 2026-08-05): постоянный WS-пуш; busy-поллинг
  // выше остаётся fallback'ом на случай разрыва. Реконнект — через 5 секунд.
  useEffect(() => {
    let unsubscribe = () => {};
    let timer: number | undefined;
    let stopped = false;
    const connect = () => {
      unsubscribe = subscribeSessionEvents(
        "",
        token,
        (event) => {
          if (event.type !== "session_title") return;
          setSessions((prev) =>
            prev.map((s) =>
              s.session_id === event.session_id
                ? { ...s, title: event.title }
                : s,
            ),
          );
        },
        () => {
          if (!stopped) timer = window.setTimeout(connect, 5000);
        },
      );
    };
    connect();
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
      unsubscribe();
    };
  }, []);
```

Топ-бар (~строка 176): заменить `{section === "chat" ? (active?.title ?? TITLES.chat) : TITLES[section]}` на:

```tsx
          {section === "chat" ? (
            // key: переключение чатов перемонтирует компонент — печатается
            // только пуш нового названия, а не каждый переход по списку.
            <AnimatedTitle
              key={activeId ?? "root"}
              text={active?.title ?? TITLES.chat}
            />
          ) : (
            TITLES[section]
          )}
```

3f. `web/src/setupTests.ts` — добавить no-op стаб (App теперь открывает WS при каждом рендере; jsdom без WebSocket падал бы во всех существующих тестах):

```typescript
// App держит постоянный WS /sessions/events; в jsdom WebSocket нет.
// No-op стаб — тесты, которым нужен живой сокет, ставят свой через
// vi.stubGlobal (паттерн ChatScreen.test.tsx).
class StubWebSocket {
  onmessage: ((ev: MessageEvent<string>) => void) | null = null;
  onclose: (() => void) | null = null;
  close(): void {}
}
vi.stubGlobal("WebSocket", StubWebSocket);
```

(Если `vi` в setupTests не импортирован — добавить `import { vi } from "vitest";`.)

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `npm --prefix web test`
Expected: PASS (tsc, prettier, vitest — все существующие + новые)

- [ ] **Step 5: Сборка бандла и коммит**

```bash
npm --prefix web run build
git add web/src
git commit -m "feat(web): живые названия чатов — WS-подписка и анимация набора"
```

(`web/dist` в .gitignore — сборка проверяет, что бандл собирается; в коммит не попадает.)

---

## Проверка вживую (после всех задач, вручную)

1. На машине с `svarog serve`: `npm --prefix web run build`, перезапустить serve.
2. Новый чат → отправить сообщение: название печатается в сайдбаре через ~1-2 сек (черновик), после ответа — дотачивается вторым набором (если изменилось).
3. Обновить страницу мид-run: после ответа название всё равно приходит (WS переподключён).
