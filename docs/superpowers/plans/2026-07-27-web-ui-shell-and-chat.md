# Веб-интерфейс «Горн»: оболочка и диалог — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Из браузера — на десктопе и на телефоне — можно открыть историю чатов, вести диалог с агентом, видеть его вызовы инструментов и решать по гейту разрешения.

**Architecture:** Клиент — SPA на Vite + React + TypeScript в `web/`, собирается в статику и раздаётся тем же `svarog serve` с того же origin. Лента строится одним рендерером из нормализованных элементов: живые события приходят по WebSocket, история сессии — одним REST-запросом, обе формы приводятся к общему типу `ThreadItem` чистой функцией.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy (сервер), Vite 6 / React 19 / TypeScript 5 / Vitest / @testing-library/react (клиент), uv и npm.

## Global Constraints

- Дизайн-спек: `docs/superpowers/specs/2026-07-27-web-ui-gorn-design.md`. Токены, правила и раскладки — оттуда, дословно.
- Ограничение ADR-0001 касается runtime: **запуск Сварога не должен требовать Node**. Бандл собирается в CI и кладётся в пакет.
- Один акцент `#D2622C` ровно в трёх местах: активная сессия, гейт, кнопка отправки. Нигде больше.
- Моноширинный шрифт только там, где код: пути, команды, диффы, имена инструментов.
- Точки перелома: ≥1240 — три колонки, 900–1239 — боковая панель схлопывается, <900 — одна колонка с выдвижным навигатором. Минимум 360 px.
- Горизонтальной прокрутки страницы нет ни на одной ширине.
- Цель нажатия на мобильном — не меньше 44 px.
- Место под микрофон в поле ввода и под воспроизведение у реплики агента присутствует в разметке, хотя голос выключен.
- Значения секретов не появляются ни в ответах API, ни в DOM.
- Питон-проверки перед каждым коммитом: `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest`.
- Комментарии и сообщения коммитов — по-русски, как в остальном репозитории.

## Расхождение со спеком, найденное при разборе кода

Спек перечисляет шесть недостающих эндпоинтов. При чтении `gateway/service.py` обнаружилось седьмое: **живой поток событий не несёт того, что нужно ленте**.

Сейчас публикуются только `{"type":"tool_call","tool":name}` — без аргументов и без результата, — а события о запросе разрешения нет вовсе (`RunHooks.on_approval_requested` в gateway не подключён, `service.py:371-380`).

При этом в БД всё есть: `ToolCall.arguments` и `ToolCall.result` (`storage/models.py:229,234`). То есть **воспроизведение истории реализуемо сразу**, а живая лента требует трёх правок на сервере — задачи 2 и 3 ниже. Спек нужно дополнить этим пунктом; правка спека входит в задачу 3.

## Декомпозиция

Спек покрывает три экрана и семь серверных доработок — это больше одного плана. Разбито на три, каждый даёт работающий софт:

1. **Этот план — оболочка и «Диалог».** Итог: из браузера можно вести диалог и решать по гейту.
2. **«Настройки»** — `GET /config` со схемой, `POST /config/preview`, `POST /config`, `GET /secrets`, экран с диффом `svarog.yaml`.
3. **«Память»** — `GET /memory/*`, экран с поиском, историей и откатом.

Планы 2 и 3 пишутся после того, как этот выполнен: они опираются на оболочку, клиент API и систему токенов из задач 3–7.

## Структура файлов

**Сервер:**

| Файл | Ответственность |
|---|---|
| `src/svarog_harness/gateway/models.py` | +`SessionSummary`, +`ThreadItemView`, +`SessionThread` |
| `src/svarog_harness/gateway/service.py` | +`list_sessions`, +`session_thread`, публикация `tool_call`/`tool_result`/`approval_required` |
| `src/svarog_harness/gateway/api.py` | +`GET /sessions`, +`GET /sessions/{id}/messages`, CORS, раздача статики |
| `src/svarog_harness/gateway/static.py` | поиск каталога собранного бандла и монтирование |
| `tests/test_gateway_web.py` | тесты всех перечисленных доработок |

**Клиент** (`web/`), по одному ответственному файлу на единицу:

| Файл | Ответственность |
|---|---|
| `src/styles/tokens.css` | токены спека, единственный источник цвета и шрифта |
| `src/api/types.ts` | типы ответов gateway, один к одному с pydantic-моделями |
| `src/api/client.ts` | HTTP: базовый URL, bearer, разбор ошибок |
| `src/api/stream.ts` | подписка на WebSocket событий run'а |
| `src/model/thread.ts` | `ThreadItem` и нормализация: события и история → один тип |
| `src/components/ToolCalls.tsx` | группа вызовов: свёрнуто, раскрыто, MCP-префикс, две строки на узком |
| `src/components/Gate.tsx` | гейт разрешения и вопрос `ask_user` |
| `src/components/Composer.tsx` | поле ввода, режимы, место под микрофон |
| `src/components/Nav.tsx` | навигатор: сессии и разделы |
| `src/components/Shell.tsx` | раскладка и выдвижной навигатор на узком экране |
| `src/screens/ChatScreen.tsx` | сборка экрана: история + стрим + отправка |

---

### Task 1: `GET /sessions` — список сессий для навигатора

**Files:**
- Modify: `src/svarog_harness/gateway/models.py`
- Modify: `src/svarog_harness/gateway/service.py`
- Modify: `src/svarog_harness/gateway/api.py`
- Test: `tests/test_gateway_web.py` (создать)

**Interfaces:**
- Consumes: `GatewayService`, `SessionView` из существующего gateway.
- Produces: `SessionSummary(session_id: str, title: str, workspace: str | None, updated_at: datetime, runs_count: int, last_state: str | None)`; `GatewayService.list_sessions(limit: int = 50) -> list[SessionSummary]`; `GET /sessions?limit=` → `list[SessionSummary]`, отсортирован по `updated_at` убыванию.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_gateway_web.py`. Фикстуры повторяют `tests/test_gateway.py` — переиспользовать их нельзя, они локальны для того модуля.

```python
"""Тесты веб-доработок gateway: список сессий, лента, статика (план 2026-07-27)."""

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app


def _write_config(ws: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "svarog.db"
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {db_path}\n",
        encoding="utf-8",
    )


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayService:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    return GatewayService(load_config(project_dir=ws), ws)


@pytest.mark.asyncio
async def test_list_sessions_newest_first(service: GatewayService) -> None:
    first = await service.create_session(title="старая")
    second = await service.create_session(title="свежая")

    listed = await service.list_sessions()

    assert [s.title for s in listed] == ["свежая", "старая"]
    assert [s.session_id for s in listed] == [second.session_id, first.session_id]
    assert listed[0].runs_count == 0
    assert listed[0].last_state is None
    assert isinstance(listed[0].updated_at, datetime)


@pytest.mark.asyncio
async def test_list_sessions_endpoint(service: GatewayService) -> None:
    await service.create_session(title="через API")
    client = TestClient(create_app(service=service))

    response = client.get("/sessions")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["title"] == "через API"
    assert body[0]["runs_count"] == 0
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `uv run pytest tests/test_gateway_web.py -v`
Expected: FAIL — `AttributeError: 'GatewayService' object has no attribute 'list_sessions'`.

- [ ] **Step 3: Добавить модель `SessionSummary`**

В `src/svarog_harness/gateway/models.py` рядом с `SessionView`:

```python
class SessionSummary(BaseModel):
    """Строка списка сессий для навигатора веб-клиента.

    Отдельно от SessionView: там полный список runs, здесь только то, что
    нужно левому столбцу, — иначе навигатор тянет весь трейс всех сессий.
    """

    session_id: str
    title: str
    workspace: str | None = None
    updated_at: datetime
    runs_count: int
    last_state: str | None = None
```

`datetime` уже импортирован в этом модуле (используется в `WorkspaceView`).

- [ ] **Step 4: Реализовать `list_sessions`**

В `src/svarog_harness/gateway/service.py` добавить метод в `GatewayService` сразу после `get_session`:

```python
    async def list_sessions(self, limit: int = 50) -> list[SessionSummary]:
        """Сессии для навигатора: свежие сверху, без полного трейса."""

        async def action(db: AsyncSession) -> list[SessionSummary]:
            found = await db.execute(
                select(Session).order_by(Session.updated_at.desc()).limit(limit)
            )
            summaries: list[SessionSummary] = []
            for session in found.scalars():
                runs = (
                    (
                        await db.execute(
                            select(Run)
                            .where(Run.session_id == session.id)
                            .order_by(Run.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                summaries.append(
                    SessionSummary(
                        session_id=session.id,
                        title=session.title or "",
                        workspace=(session.meta or {}).get("workspace"),
                        updated_at=session.updated_at,
                        runs_count=len(runs),
                        last_state=runs[-1].state.value if runs else None,
                    )
                )
            return summaries

        return await self._read(action)
```

Добавить `SessionSummary` в блок импорта из `svarog_harness.gateway.models` в начале файла и в `__all__` в конце.

- [ ] **Step 5: Добавить эндпоинт**

В `src/svarog_harness/gateway/api.py`, сразу перед `@app.get("/sessions/{session_id}")` — порядок важен, иначе `/sessions` попадёт в маршрут с параметром:

```python
    @app.get("/sessions", response_model=list[SessionSummary])
    async def list_sessions(service: ServiceDep, limit: int = 50) -> list[SessionSummary]:
        return await service.list_sessions(limit=limit)
```

`SessionSummary` добавить в импорт моделей вверху файла.

- [ ] **Step 6: Убедиться, что тесты проходят**

Run: `uv run pytest tests/test_gateway_web.py -v`
Expected: PASS, оба теста.

- [ ] **Step 7: Прогнать полный набор проверок**

Run: `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest`
Expected: всё зелёное.

- [ ] **Step 8: Коммит**

```bash
git add src/svarog_harness/gateway/models.py src/svarog_harness/gateway/service.py src/svarog_harness/gateway/api.py tests/test_gateway_web.py
git commit -m "feat(gateway): GET /sessions — список сессий для навигатора"
```

---

### Task 2: Аргументы и результаты вызовов в живом потоке

**Files:**
- Create: `src/svarog_harness/runtime/summaries.py`
- Modify: `src/svarog_harness/runtime/run_assembly.py:95` (рядом с `on_tool_call`)
- Modify: `src/svarog_harness/gateway/service.py:371-380`
- Test: `tests/test_gateway_web.py`

**Interfaces:**
- Consumes: существующий `run_assembly.RunHooks`.
- Produces: `runtime/summaries.py` с `short_arg(arguments: dict[str, Any]) -> str` и `short_result(*, ok: bool, output: str, error: str | None = None) -> str`; новый хук `RunHooks.on_tool_result: Callable[[str, str, str], None] | None` с аргументами `(tool_name, status, summary)`; события потока `{"type":"tool_call","tool":str,"arg":str}` и `{"type":"tool_result","tool":str,"status":str,"result":str}`.

> **Поправка, внесённая при выполнении.** Черновик плана считал, что результат
> вызова — словарь вида `{"added": 58, "removed": 4}`, и обещал справа `+58 −4`.
> На деле `ToolResult.output` — строка (`tools/base.py:64`), а в БД всегда
> ложится `{"output": <текст>}` (`trace/recorder.py:158`): инструменты
> возвращают прозу — «записано 1234 символов в memory/index.py», «заменено
> вхождений: 3», сырой stdout. Числа взять неоткуда. Принято решение показывать
> **первую непустую строку вывода**, обрезанную до 60 символов, а для упавшего
> вызова — текст ошибки. Структурированный итог инструментов — отдельный план,
> см. «Что этот план не закрывает».

**Где живут сокращатели:** в `runtime/`, а не в `gateway/`. Хук зовёт runtime, и сводку считает вызывающая сторона — если положить функции в gateway, runtime не сможет их импортировать, не развернув зависимость наизнанку. Telegram-интерфейс и CLI получают их даром.

**Почему так:** лента показывает у каждого вызова аргумент слева и результат справа. Сейчас в поток уходит только имя, поэтому живая лента не может отрисовать то, что нарисовано в макете, а после перезагрузки страницы те же вызовы приезжают из истории с полным содержимым — лента «доедет» и изменится на глазах.

- [ ] **Step 1: Написать падающий тест**

Дописать в `tests/test_gateway_web.py`:

```python
from svarog_harness.runtime.summaries import short_arg, short_result


def test_short_arg_prefers_meaningful_key() -> None:
    assert short_arg({"path": "memory/index.py", "content": "x" * 500}) == "memory/index.py"
    assert short_arg({"command": "uv run pytest -q"}) == "uv run pytest -q"
    assert short_arg({"query": "стоп-слова префикс"}) == "стоп-слова префикс"
    assert short_arg({}) == ""


def test_short_arg_truncates_long_values() -> None:
    assert short_arg({"path": "a" * 200}) == "a" * 119 + "…"


def test_short_result_reports_counts_and_diff() -> None:
    assert short_result({"added": 58, "removed": 4}) == "+58 −4"
    assert short_result({"matches": 3}) == "3 совпадения"
    assert short_result({"exit_code": 1}) == "код 1"
    assert short_result({}) == ""
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `uv run pytest tests/test_gateway_web.py -k short -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.runtime.summaries'`.

- [ ] **Step 3: Реализовать сокращатели**

Создать `src/svarog_harness/runtime/summaries.py`:

```python
"""Короткие сводки вызова инструмента для интерфейсов.

Строка вызова в ленте вмещает один аргумент слева и один результат справа.
Считать их должен runtime: хук `on_tool_result` зовётся из цикла, а не из
gateway, поэтому обратная зависимость невозможна.
"""

from typing import Any

_ARG_KEYS = ("path", "command", "query", "url", "name", "branch")
_ARG_LIMIT = 120


def short_arg(arguments: dict[str, Any]) -> str:
    """Один осмысленный аргумент для строки вызова в ленте.

    В строке помещается ровно одно значение, поэтому берётся первый из
    известных ключей, а не сериализация всего словаря: путь и команда
    опознаются человеком с одного взгляда, `{"content": "..."}` — нет.
    """
    for key in _ARG_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value if len(value) <= _ARG_LIMIT else value[: _ARG_LIMIT - 1] + "…"
    return ""


def short_result(result: dict[str, Any]) -> str:
    """Результат вызова так, как он стоит справа в строке ленты.

    Слово «успешно» не несёт информации и занимает место, где могло бы
    стоять число, — поэтому возвращаются только измеримые исходы.
    """
    added, removed = result.get("added"), result.get("removed")
    if isinstance(added, int) and isinstance(removed, int):
        return f"+{added} −{removed}"
    matches = result.get("matches")
    if isinstance(matches, int):
        return f"{matches} совпадения"
    exit_code = result.get("exit_code")
    if isinstance(exit_code, int):
        return f"код {exit_code}"
    return ""
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest tests/test_gateway_web.py -k short -v`
Expected: PASS, три теста.

- [ ] **Step 5: Написать падающий тест на публикацию событий**

Дописать в `tests/test_gateway_web.py`:

```python
import asyncio

from svarog_harness.gateway.service import _RunHolder


@pytest.mark.asyncio
async def test_tool_events_carry_arg_and_result(service: GatewayService) -> None:
    published: list[dict[str, object]] = []
    service.events.publish = lambda run_id, event: published.append(event)  # type: ignore[method-assign]

    started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    holder = _RunHolder()
    holder.run_id = "run-1"
    hooks = service._event_hooks(holder, started)

    assert hooks.on_tool_call is not None
    assert hooks.on_tool_result is not None
    hooks.on_tool_call("write_file", {"path": "memory/index.py", "content": "x"})
    hooks.on_tool_result("write_file", "succeeded", "+58 −4")

    assert published == [
        {"type": "tool_call", "tool": "write_file", "arg": "memory/index.py"},
        {"type": "tool_result", "tool": "write_file", "status": "succeeded", "result": "+58 −4"},
    ]
```

- [ ] **Step 6: Убедиться, что тест падает**

Run: `uv run pytest tests/test_gateway_web.py -k tool_events -v`
Expected: FAIL — `AttributeError: 'RunHooks' object has no attribute 'on_tool_result'`.

- [ ] **Step 7: Добавить хук**

В `src/svarog_harness/runtime/run_assembly.py`, сразу после `on_tool_call` (строка 95):

```python
    # Результат вызова: (tool_name, status, короткая сводка). Нужен интерфейсам,
    # которые показывают исход рядом с вызовом, — в БД он есть (ToolCall.result),
    # но по ходу прогона наблюдателю недоступен.
    on_tool_result: Callable[[str, str, str], None] | None = None
```

- [ ] **Step 8: Публиковать аргумент и результат**

В `src/svarog_harness/gateway/service.py` заменить строку с `on_tool_call` внутри `_event_hooks` и добавить `on_tool_result`:

```python
        return RunHooks(
            on_run_started=on_started,
            on_text_delta=lambda delta: emit({"type": "text", "delta": delta}),
            on_tool_call=lambda name, args: emit(
                {"type": "tool_call", "tool": name, "arg": short_arg(args)}
            ),
            on_tool_result=lambda name, status, summary: emit(
                {"type": "tool_result", "tool": name, "status": status, "result": summary}
            ),
            on_notify=lambda name, reason: emit({"type": "notify", "tool": name, "reason": reason}),
            on_check=on_check,
            on_commit=lambda sha, branch, push: emit(
                {"type": "commit", "sha": sha, "branch": branch}
            ),
        )
```

- [ ] **Step 9: Вызвать хук из цикла**

Найти место, где `on_tool_call` дёргается в цикле выполнения:

Run: `grep -rn "on_tool_call" src/svarog_harness/runtime/`

В том же файле, сразу после того как вызов инструмента завершён и записан его результат, добавить симметричный вызов:

```python
        if hooks.on_tool_result is not None:
            hooks.on_tool_result(
                call.tool_name, call.status.value, short_result(call.result or {})
            )
```

Импорт в начале того же файла:

```python
from svarog_harness.runtime.summaries import short_result
```

Имена `call.status` и `call.result` взяты из ORM-модели `ToolCall` (`storage/models.py:233-234`). Если в найденном месте объект называется иначе, использовать его поля — важно передать статус строкой и результат словарём.

В `src/svarog_harness/gateway/service.py` добавить импорт `short_arg`:

```python
from svarog_harness.runtime.summaries import short_arg
```

- [ ] **Step 10: Убедиться, что тесты проходят**

Run: `uv run pytest tests/test_gateway_web.py -v`
Expected: PASS, все тесты задач 1 и 2.

- [ ] **Step 11: Прогнать полный набор проверок**

Run: `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest`
Expected: всё зелёное. Существующие тесты в `tests/test_gateway.py` проверяют состав событий — если какой-то сравнивает список событий целиком, обновить ожидание: у `tool_call` появилось поле `arg`.

- [ ] **Step 12: Коммит**

```bash
git add src/svarog_harness/runtime/ src/svarog_harness/gateway/service.py tests/test_gateway_web.py
git commit -m "feat(gateway): аргумент и результат вызова в потоке событий"
```

---

### Task 3: Событие о запросе разрешения + правка спека

**Files:**
- Modify: `src/svarog_harness/gateway/service.py` (`_event_hooks`)
- Modify: `docs/superpowers/specs/2026-07-27-web-ui-gorn-design.md` (список недостающего)
- Test: `tests/test_gateway_web.py`

**Interfaces:**
- Consumes: `RunHooks.on_approval_requested: Callable[[Approval], None] | None` (`run_assembly.py:109`), уже существует и в gateway не подключён.
- Produces: событие потока `{"type":"approval_required","approval_id":str,"action_type":str,"payload":dict}`.

**Почему так:** без этого события гейт в ленте появляется только после отдельного запроса `GET /approvals`, то есть с задержкой опроса. Хук уже есть — не подключён.

- [ ] **Step 1: Написать падающий тест**

```python
@pytest.mark.asyncio
async def test_approval_event_published(service: GatewayService) -> None:
    import asyncio

    from svarog_harness.gateway.service import _RunHolder
    from svarog_harness.storage.models import Approval

    published: list[dict[str, object]] = []
    service.events.publish = lambda run_id, event: published.append(event)  # type: ignore[method-assign]

    started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    holder = _RunHolder()
    holder.run_id = "run-1"
    hooks = service._event_hooks(holder, started)

    approval = Approval(
        id="ap-1",
        run_id="run-1",
        action_type="run_shell",
        payload={"command": "uv run pytest -q"},
    )
    assert hooks.on_approval_requested is not None
    hooks.on_approval_requested(approval)

    assert published == [
        {
            "type": "approval_required",
            "approval_id": "ap-1",
            "action_type": "run_shell",
            "payload": {"command": "uv run pytest -q"},
        }
    ]
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `uv run pytest tests/test_gateway_web.py -k approval_event -v`
Expected: FAIL — `assert hooks.on_approval_requested is not None` не выполняется (хук `None`).

- [ ] **Step 3: Подключить хук**

В `_event_hooks` добавить в возвращаемый `RunHooks`:

```python
            # Гейт появляется в ленте сразу, а не по опросу /approvals.
            on_approval_requested=lambda approval: emit(
                {
                    "type": "approval_required",
                    "approval_id": approval.id,
                    "action_type": approval.action_type,
                    "payload": approval.payload or {},
                }
            ),
```

Импортировать `Approval` из `svarog_harness.storage.models` — в этом файле уже импортируются `Run`, `RunState`, `Session` из того же модуля, добавить в тот же блок.

- [ ] **Step 4: Убедиться, что тест проходит**

Run: `uv run pytest tests/test_gateway_web.py -k approval_event -v`
Expected: PASS.

- [ ] **Step 5: Дополнить спек найденным расхождением**

В `docs/superpowers/specs/2026-07-27-web-ui-gorn-design.md`, в раздел «Чего не хватает — новые эндпоинты», добавить седьмым пунктом:

```markdown
7. **Поток событий не несёт содержимого вызовов.** Публикуется только
   `{"type":"tool_call","tool":name}` — без аргумента и результата, — а
   события о запросе разрешения нет вовсе, хотя хук
   `RunHooks.on_approval_requested` существует. В БД содержимое есть
   (`ToolCall.arguments`, `ToolCall.result`), поэтому воспроизведение
   истории работает, а живая лента без этой правки показывает вызовы
   иначе, чем они выглядят после перезагрузки страницы.
```

- [ ] **Step 6: Прогнать полный набор проверок**

Run: `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest`
Expected: всё зелёное.

- [ ] **Step 7: Коммит**

```bash
git add src/svarog_harness/gateway/service.py tests/test_gateway_web.py docs/superpowers/specs/2026-07-27-web-ui-gorn-design.md
git commit -m "feat(gateway): событие approval_required + дополнение спека"
```

---

### Task 4: `GET /sessions/{id}/messages` — воспроизведение ленты

**Files:**
- Modify: `src/svarog_harness/gateway/models.py`
- Modify: `src/svarog_harness/gateway/service.py`
- Modify: `src/svarog_harness/gateway/api.py`
- Test: `tests/test_gateway_web.py`

**Interfaces:**
- Consumes: `ToolCall.arguments`, `ToolCall.result`, `ToolCall.status` (`storage/models.py:229-234`); `TraceRecorder.last_assistant_text`; `short_arg`, `short_result` из `runtime/summaries.py` (задача 2).
- Produces: `ThreadItemView(kind: Literal["user","say","call"], text: str = "", server: str | None = None, name: str = "", arg: str = "", result: str = "", status: str = "")`; `SessionThread(session_id: str, title: str, items: list[ThreadItemView])`; `GatewayService.session_thread(session_id: str) -> SessionThread`; `GET /sessions/{id}/messages`.

**Почему одна форма:** живая лента и воспроизведённая рисуются одним компонентом. Если сервер отдаст историю в другой форме, придётся писать второй рендерер, и они разойдутся — это записано в спеке как риск.

- [ ] **Step 1: Написать падающий тест**

```python
@pytest.mark.asyncio
async def test_session_thread_replays_user_calls_and_answer(service: GatewayService) -> None:
    from sqlalchemy import select

    from svarog_harness.storage.models import Run, RunState, ToolCall, ToolCallStatus

    session = await service.create_session(title="лента")

    async def seed(db):  # type: ignore[no-untyped-def]
        run = Run(
            id="run-1",
            session_id=session.session_id,
            task="Добавь FTS-поиск",
            state=RunState.SUCCEEDED,
            autonomy="supervised",
            iterations=1,
            tokens_used=0,
            cost_usd=0.0,
        )
        db.add(run)
        db.add(
            ToolCall(
                id="tc-1",
                run_id="run-1",
                tool_name="write_file",
                arguments={"path": "memory/index.py"},
                result={"added": 58, "removed": 4},
                status=ToolCallStatus.SUCCEEDED,
            )
        )
        await db.flush()
        return None

    await service._write(seed)  # type: ignore[attr-defined]

    thread = await service.session_thread(session.session_id)

    kinds = [item.kind for item in thread.items]
    assert kinds[0] == "user"
    assert thread.items[0].text == "Добавь FTS-поиск"
    call = next(item for item in thread.items if item.kind == "call")
    assert call.name == "write_file"
    assert call.arg == "memory/index.py"
    assert call.result == "+58 −4"
    assert call.status == "succeeded"


@pytest.mark.asyncio
async def test_session_thread_endpoint_404(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))
    assert client.get("/sessions/нет-такой/messages").status_code == 404
```

Метод записи в БД в `GatewayService` может называться иначе, чем `_write`. Перед реализацией выяснить:

Run: `grep -n "async def _read\|async def _write\|def _session_scope" src/svarog_harness/gateway/service.py`

и использовать найденное имя в тесте и в реализации.

- [ ] **Step 2: Убедиться, что тест падает**

Run: `uv run pytest tests/test_gateway_web.py -k session_thread -v`
Expected: FAIL — `AttributeError: 'GatewayService' object has no attribute 'session_thread'`.

- [ ] **Step 3: Добавить модели**

В `src/svarog_harness/gateway/models.py`:

```python
class ThreadItemView(BaseModel):
    """Элемент ленты в той же форме, в какой его собирает живой поток.

    Один тип на реплику, речь агента и вызов инструмента: клиент рисует
    историю и живые события одним компонентом, иначе они разойдутся.
    """

    kind: Literal["user", "say", "call"]
    text: str = ""
    server: str | None = None  # имя MCP-сервера; None — свой инструмент
    name: str = ""
    arg: str = ""
    result: str = ""
    status: str = ""


class SessionThread(BaseModel):
    session_id: str
    title: str
    items: list[ThreadItemView]
```

Добавить `Literal` в импорт из `typing` вверху файла.

- [ ] **Step 4: Реализовать `session_thread`**

В `GatewayService`, после `list_sessions`:

```python
    async def session_thread(self, session_id: str) -> SessionThread:
        """История сессии как лента: задача, вызовы, финальный ответ по каждому run."""

        async def action(db: AsyncSession) -> SessionThread:
            session = await find_session_by_prefix(db, session_id)
            runs = (
                (
                    await db.execute(
                        select(Run).where(Run.session_id == session.id).order_by(Run.created_at)
                    )
                )
                .scalars()
                .all()
            )
            recorder = TraceRecorder(db)
            items: list[ThreadItemView] = []
            for run in runs:
                items.append(ThreadItemView(kind="user", text=run.task))
                calls = (
                    (
                        await db.execute(
                            select(ToolCall)
                            .where(ToolCall.run_id == run.id)
                            .order_by(ToolCall.started_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                for call in calls:
                    server, _, bare = call.tool_name.rpartition("/")
                    items.append(
                        ThreadItemView(
                            kind="call",
                            server=server or None,
                            name=bare,
                            arg=short_arg(call.arguments or {}),
                            result=short_result(
                                ok=call.status is ToolCallStatus.SUCCEEDED,
                                output=str((call.result or {}).get("output", "")),
                                error=call.error,
                            ),
                            status=call.status.value,
                        )
                    )
                answer = await recorder.last_assistant_text(run)
                if answer:
                    items.append(ThreadItemView(kind="say", text=answer))
            return SessionThread(
                session_id=session.id, title=session.title or "", items=items
            )

        return await self._read(action)
```

Добавить в импорты: `ToolCall` и `ToolCallStatus` из `svarog_harness.storage.models`, `ThreadItemView` и `SessionThread` из моделей gateway, `short_result` из `svarog_harness.runtime.summaries` (`short_arg` импортирован в задаче 2). `TraceRecorder` и `find_session_by_prefix` в файле уже используются.

- [ ] **Step 5: Добавить эндпоинт**

В `api.py`, после `get_session`:

```python
    @app.get("/sessions/{session_id}/messages", response_model=SessionThread)
    async def session_messages(session_id: str, service: ServiceDep) -> SessionThread:
        try:
            return await service.session_thread(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
```

- [ ] **Step 6: Убедиться, что тесты проходят**

Run: `uv run pytest tests/test_gateway_web.py -v`
Expected: PASS.

- [ ] **Step 7: Прогнать полный набор проверок**

Run: `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest`
Expected: всё зелёное.

- [ ] **Step 8: Коммит**

```bash
git add src/svarog_harness/gateway/ tests/test_gateway_web.py
git commit -m "feat(gateway): GET /sessions/{id}/messages — воспроизведение ленты"
```

---

### Task 5: Скелет клиента, токены и место в CI

**Files:**
- Create: `web/package.json`, `web/tsconfig.json`, `web/vite.config.ts`, `web/index.html`, `web/src/main.tsx`, `web/src/App.tsx`, `web/src/styles/tokens.css`, `web/src/styles/base.css`, `web/src/vite-env.d.ts`
- Create: `web/src/styles/tokens.test.ts`
- Modify: `.github/workflows/ci.yml`
- Modify: `.gitignore`

**Interfaces:**
- Produces: рабочие команды `npm --prefix web ci`, `npm --prefix web test`, `npm --prefix web run build` (кладёт бандл в `web/dist`); CSS-переменные токенов на `:root`.

- [ ] **Step 1: Создать `web/package.json`**

```json
{
  "name": "svarog-web",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "test": "vitest run",
    "lint": "tsc --noEmit"
  },
  "dependencies": {
    "react": "^19.0.0",
    "react-dom": "^19.0.0"
  },
  "devDependencies": {
    "@testing-library/jest-dom": "^6.6.3",
    "@testing-library/react": "^16.1.0",
    "@types/react": "^19.0.0",
    "@types/react-dom": "^19.0.0",
    "@vitejs/plugin-react": "^4.3.4",
    "jsdom": "^25.0.1",
    "typescript": "^5.7.2",
    "vite": "^6.0.0",
    "vitest": "^2.1.8"
  }
}
```

- [ ] **Step 2: Создать конфигурацию**

`web/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noEmit": true,
    "skipLibCheck": true,
    "types": ["vitest/globals", "@testing-library/jest-dom"]
  },
  "include": ["src"]
}
```

`web/vite.config.ts`:

```ts
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  // Бандл раздаётся тем же svarog serve с того же origin — базовый путь корневой.
  base: '/',
  server: {
    // Режим раздельной разработки: API живёт на gateway, а не на dev-сервере.
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/setupTests.ts'],
  },
})
```

`web/src/setupTests.ts`:

```ts
import '@testing-library/jest-dom/vitest'
```

`web/src/vite-env.d.ts`:

```ts
/// <reference types="vite/client" />
```

`web/index.html`:

```html
<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <title>Сварог</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 3: Создать токены**

`web/src/styles/tokens.css` — значения дословно из спека, ничего не додумывать:

```css
:root {
  --bg: #1a1917;
  --surface: #211f1d;
  --raised: #292724;
  --line: #322f2b;
  --line-soft: #262421;

  --text: #eae5dc;
  --muted: #a29b90;
  --faint: #6e6862;

  /* Акцент: активная сессия, гейт, отправка. Больше нигде. */
  --ember: #d2622c;

  --ok: #6e9b72;
  --bad: #c4635c;
  --git: #7e93b8;

  --sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  --mono: ui-monospace, 'SF Mono', SFMono-Regular, Menlo, monospace;

  /* Точки перелома спека. */
  --bp-wide: 1240px;
  --bp-narrow: 900px;
}
```

`web/src/styles/base.css`:

```css
@import './tokens.css';

* {
  box-sizing: border-box;
}

html,
body,
#root {
  height: 100%;
  margin: 0;
}

body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--sans);
  font-size: 15px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  /* Горизонтальной прокрутки страницы нет ни на одной ширине. */
  overflow-x: hidden;
}

:focus-visible {
  outline: 2px solid var(--ember);
  outline-offset: 2px;
}

@media (prefers-reduced-motion: reduce) {
  * {
    animation: none !important;
    transition: none !important;
  }
}
```

- [ ] **Step 4: Написать падающий тест на токены**

`web/src/styles/tokens.test.ts` — тест ловит расхождение с утверждённым спеком, а не наличие файла:

```ts
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('./tokens.css', import.meta.url), 'utf8')

describe('токены', () => {
  it('совпадают со спеком', () => {
    const expected: Record<string, string> = {
      '--bg': '#1a1917',
      '--surface': '#211f1d',
      '--raised': '#292724',
      '--line': '#322f2b',
      '--line-soft': '#262421',
      '--text': '#eae5dc',
      '--muted': '#a29b90',
      '--faint': '#6e6862',
      '--ember': '#d2622c',
      '--ok': '#6e9b72',
      '--bad': '#c4635c',
      '--git': '#7e93b8',
    }
    for (const [name, value] of Object.entries(expected)) {
      expect(css).toContain(`${name}: ${value};`)
    }
  })

  it('не содержит второго акцентного оранжевого', () => {
    const oranges = css.match(/#[dD][0-9a-fA-F]{5}/g) ?? []
    expect(oranges).toEqual(['#d2622c'])
  })
})
```

- [ ] **Step 5: Создать точку входа**

`web/src/App.tsx`:

```tsx
export function App() {
  return <div>Сварог</div>
}
```

`web/src/main.tsx`:

```tsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from './App'
import './styles/base.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
```

- [ ] **Step 6: Установить и прогнать**

```bash
cd web && npm install && npm test && npm run build
```

Expected: тесты токенов проходят, `web/dist/index.html` создан.

- [ ] **Step 7: Добавить `web/` в `.gitignore`**

Дописать в `.gitignore`:

```gitignore
# Клиент: зависимости и сборка не коммитятся, бандл собирает CI
web/node_modules/
web/dist/
```

- [ ] **Step 8: Добавить шаг в CI**

В `.github/workflows/ci.yml`, после шага «Tests», добавить:

```yaml
      - name: Setup Node
        uses: actions/setup-node@v4
        with:
          node-version: "22"
          cache: npm
          cache-dependency-path: web/package-lock.json

      - name: Web install
        run: npm --prefix web ci

      - name: Web typecheck and tests
        run: npm --prefix web test

      - name: Web build
        run: npm --prefix web run build
```

- [ ] **Step 9: Коммит**

```bash
git add web/ .gitignore .github/workflows/ci.yml
git commit -m "feat(web): скелет клиента Vite+React+TS, токены и шаг в CI"
```

---

### Task 6: Типы и клиент API

**Files:**
- Create: `web/src/api/types.ts`, `web/src/api/client.ts`, `web/src/api/client.test.ts`

**Interfaces:**
- Consumes: `GET /sessions`, `GET /sessions/{id}/messages`, `POST /sessions`, `POST /sessions/{id}/messages`, `POST /approvals/{id}` — из задач 1 и 4 и существующего gateway.
- Produces: типы `SessionSummary`, `ThreadItemView`, `SessionThread`, `RunRef`; класс `ApiError extends Error { status: number }`; `createClient(opts: { baseUrl: string; token?: string }): Api` с методами `listSessions()`, `sessionThread(id)`, `createSession(title)`, `sendMessage(id, text)`, `decideApproval(approvalId, approved)`.

- [ ] **Step 1: Написать падающий тест**

`web/src/api/client.test.ts`:

```ts
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, createClient } from './client'

describe('клиент API', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('подставляет bearer и базовый URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([]), { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const api = createClient({ baseUrl: 'http://svarog.test', token: 'секрет' })
    await api.listSessions()

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('http://svarog.test/sessions')
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer секрет')
  })

  it('не шлёт заголовок авторизации без токена', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('[]', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await createClient({ baseUrl: '' }).listSessions()

    const [, init] = fetchMock.mock.calls[0]
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined()
  })

  it('превращает ошибку сервера в ApiError с текстом detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'нет такой сессии' }), { status: 404 }),
      ),
    )

    const api = createClient({ baseUrl: '' })

    await expect(api.sessionThread('нет')).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
      message: 'нет такой сессии',
    })
    await expect(api.sessionThread('нет')).rejects.toBeInstanceOf(ApiError)
  })
})
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `npm --prefix web test`
Expected: FAIL — не найден модуль `./client`.

- [ ] **Step 3: Написать типы**

`web/src/api/types.ts`:

```ts
/** Один к одному с pydantic-моделями gateway. Расхождение здесь — ошибка. */

export interface SessionSummary {
  session_id: string
  title: string
  workspace: string | null
  updated_at: string
  runs_count: number
  last_state: string | null
}

export interface ThreadItemView {
  kind: 'user' | 'say' | 'call'
  text: string
  server: string | null
  name: string
  arg: string
  result: string
  status: string
}

export interface SessionThread {
  session_id: string
  title: string
  items: ThreadItemView[]
}

export interface RunRef {
  run_id: string
  state: string
}
```

- [ ] **Step 4: Написать клиент**

`web/src/api/client.ts`:

```ts
import type { RunRef, SessionSummary, SessionThread } from './types'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export interface ClientOptions {
  baseUrl: string
  token?: string
}

export interface Api {
  listSessions(): Promise<SessionSummary[]>
  sessionThread(sessionId: string): Promise<SessionThread>
  createSession(title: string): Promise<{ session_id: string }>
  sendMessage(sessionId: string, text: string): Promise<RunRef>
  decideApproval(approvalId: string, approved: boolean): Promise<RunRef>
}

export function createClient({ baseUrl, token }: ClientOptions): Api {
  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers: Record<string, string> = { 'content-type': 'application/json' }
    if (token) headers.Authorization = `Bearer ${token}`
    const response = await fetch(`${baseUrl}${path}`, { ...init, headers })
    if (!response.ok) {
      // detail — стандартная форма ошибки FastAPI; без него берём статус.
      const body = await response.json().catch(() => null)
      const detail = body && typeof body.detail === 'string' ? body.detail : response.statusText
      throw new ApiError(response.status, detail)
    }
    return (await response.json()) as T
  }

  return {
    listSessions: () => request<SessionSummary[]>('/sessions'),
    sessionThread: (sessionId) =>
      request<SessionThread>(`/sessions/${encodeURIComponent(sessionId)}/messages`),
    createSession: (title) =>
      request<{ session_id: string }>('/sessions', {
        method: 'POST',
        body: JSON.stringify({ title }),
      }),
    sendMessage: (sessionId, text) =>
      request<RunRef>(`/sessions/${encodeURIComponent(sessionId)}/messages`, {
        method: 'POST',
        body: JSON.stringify({ text }),
      }),
    decideApproval: (approvalId, approved) =>
      request<RunRef>(`/approvals/${encodeURIComponent(approvalId)}`, {
        method: 'POST',
        body: JSON.stringify({ approved }),
      }),
  }
}
```

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `npm --prefix web test`
Expected: PASS, три теста клиента и два теста токенов.

- [ ] **Step 6: Коммит**

```bash
git add web/src/api/
git commit -m "feat(web): типы gateway и клиент API"
```

---

### Task 7: Нормализация ленты

**Files:**
- Create: `web/src/model/thread.ts`, `web/src/model/thread.test.ts`

**Interfaces:**
- Consumes: `ThreadItemView` из `web/src/api/types.ts` (задача 6); события потока из задач 2 и 3.
- Produces:

```ts
export type ThreadItem =
  | { kind: 'user'; id: string; text: string }
  | { kind: 'say'; id: string; text: string }
  | { kind: 'call'; id: string; server: string | null; name: string; arg: string; result: string; status: 'ok' | 'run' | 'error' }
  | { kind: 'gate'; id: string; approvalId: string; actionType: string; command: string }

export function fromHistory(items: ThreadItemView[]): ThreadItem[]
export function applyEvent(items: ThreadItem[], event: StreamEvent): ThreadItem[]
```

**Почему чистые функции:** живая лента и воспроизведённая обязаны совпадать — это записанный в спеке риск. Сравнить их можно только тестом, а тест возможен, только если превращение не размазано по компонентам.

- [ ] **Step 1: Написать падающий тест**

`web/src/model/thread.test.ts`:

```ts
import { describe, expect, it } from 'vitest'

import type { ThreadItemView } from '../api/types'
import { applyEvent, fromHistory } from './thread'

const view = (over: Partial<ThreadItemView>): ThreadItemView => ({
  kind: 'call',
  text: '',
  server: null,
  name: '',
  arg: '',
  result: '',
  status: '',
  ...over,
})

describe('нормализация ленты', () => {
  it('переносит историю без потерь', () => {
    const items = fromHistory([
      view({ kind: 'user', text: 'Добавь FTS-поиск' }),
      view({ name: 'write_file', arg: 'memory/index.py', result: '+58 −4', status: 'succeeded' }),
      view({ kind: 'say', text: 'Готово' }),
    ])

    expect(items.map((item) => item.kind)).toEqual(['user', 'call', 'say'])
    expect(items[1]).toMatchObject({
      kind: 'call',
      name: 'write_file',
      arg: 'memory/index.py',
      result: '+58 −4',
      status: 'ok',
    })
  })

  it('переводит статусы вызова в три состояния ленты', () => {
    const statuses = ['succeeded', 'running', 'failed', 'denied'].map(
      (status) => (fromHistory([view({ name: 't', status })])[0] as { status: string }).status,
    )
    expect(statuses).toEqual(['ok', 'run', 'error', 'error'])
  })

  it('живой поток даёт ту же ленту, что и история', () => {
    const live = [
      { type: 'tool_call', tool: 'write_file', arg: 'memory/index.py' },
      { type: 'tool_result', tool: 'write_file', status: 'succeeded', result: '+58 −4' },
    ].reduce(applyEvent, [] as ReturnType<typeof fromHistory>)

    const replayed = fromHistory([
      view({ name: 'write_file', arg: 'memory/index.py', result: '+58 −4', status: 'succeeded' }),
    ])

    expect(live.map(({ id: _id, ...rest }) => rest)).toEqual(
      replayed.map(({ id: _id, ...rest }) => rest),
    )
  })

  it('склеивает text-дельты в одну реплику', () => {
    const items = [
      { type: 'text', delta: 'Точный проход ' },
      { type: 'text', delta: 'идёт первым.' },
    ].reduce(applyEvent, [] as ReturnType<typeof fromHistory>)

    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({ kind: 'say', text: 'Точный проход идёт первым.' })
  })

  it('отделяет MCP-сервер от имени инструмента', () => {
    const items = applyEvent([], {
      type: 'tool_call',
      tool: 'github/list_issues',
      arg: 'label: memory',
    })
    expect(items[0]).toMatchObject({ server: 'github', name: 'list_issues' })
  })

  it('добавляет гейт по событию approval_required', () => {
    const items = applyEvent([], {
      type: 'approval_required',
      approval_id: 'ap-1',
      action_type: 'run_shell',
      payload: { command: 'uv run pytest -q' },
    })
    expect(items[0]).toMatchObject({
      kind: 'gate',
      approvalId: 'ap-1',
      command: 'uv run pytest -q',
    })
  })
})
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `npm --prefix web test`
Expected: FAIL — не найден модуль `./thread`.

- [ ] **Step 3: Реализовать нормализацию**

`web/src/model/thread.ts`:

```ts
import type { ThreadItemView } from '../api/types'

export type CallStatus = 'ok' | 'run' | 'error'

export type ThreadItem =
  | { kind: 'user'; id: string; text: string }
  | { kind: 'say'; id: string; text: string }
  | {
      kind: 'call'
      id: string
      server: string | null
      name: string
      arg: string
      result: string
      status: CallStatus
    }
  | { kind: 'gate'; id: string; approvalId: string; actionType: string; command: string }

export type StreamEvent =
  | { type: 'text'; delta: string }
  | { type: 'tool_call'; tool: string; arg?: string }
  | { type: 'tool_result'; tool: string; status: string; result?: string }
  | { type: 'approval_required'; approval_id: string; action_type: string; payload: Record<string, unknown> }
  | { type: string; [key: string]: unknown }

let counter = 0
const nextId = () => `i${(counter += 1)}`

/** succeeded → ok, running → run, всё остальное (failed, denied) → error. */
function toStatus(raw: string): CallStatus {
  if (raw === 'running') return 'run'
  if (raw === 'succeeded') return 'ok'
  return 'error'
}

/** `github/list_issues` → сервер и имя; свой инструмент — сервер null. */
function splitTool(tool: string): { server: string | null; name: string } {
  const at = tool.lastIndexOf('/')
  if (at < 0) return { server: null, name: tool }
  return { server: tool.slice(0, at), name: tool.slice(at + 1) }
}

export function fromHistory(items: ThreadItemView[]): ThreadItem[] {
  return items.map((item) => {
    if (item.kind === 'user') return { kind: 'user', id: nextId(), text: item.text }
    if (item.kind === 'say') return { kind: 'say', id: nextId(), text: item.text }
    return {
      kind: 'call',
      id: nextId(),
      server: item.server,
      name: item.name,
      arg: item.arg,
      result: item.result,
      status: toStatus(item.status),
    }
  })
}

export function applyEvent(items: ThreadItem[], event: StreamEvent): ThreadItem[] {
  if (event.type === 'text') {
    const delta = String((event as { delta: string }).delta)
    const last = items[items.length - 1]
    // Дельты одной реплики склеиваются, иначе лента распадается на слова.
    if (last?.kind === 'say') {
      return [...items.slice(0, -1), { ...last, text: last.text + delta }]
    }
    return [...items, { kind: 'say', id: nextId(), text: delta }]
  }

  if (event.type === 'tool_call') {
    const { tool, arg } = event as { tool: string; arg?: string }
    const { server, name } = splitTool(tool)
    return [
      ...items,
      { kind: 'call', id: nextId(), server, name, arg: arg ?? '', result: '', status: 'run' },
    ]
  }

  if (event.type === 'tool_result') {
    const { tool, status, result } = event as { tool: string; status: string; result?: string }
    const { name } = splitTool(tool)
    // Результат приходит отдельным событием — дописывается в последний
    // незавершённый вызов с тем же именем, а не создаёт вторую строку.
    for (let i = items.length - 1; i >= 0; i -= 1) {
      const item = items[i]
      if (item.kind === 'call' && item.name === name && item.status === 'run') {
        const patched: ThreadItem = { ...item, status: toStatus(status), result: result ?? '' }
        return [...items.slice(0, i), patched, ...items.slice(i + 1)]
      }
    }
    return items
  }

  if (event.type === 'approval_required') {
    const { approval_id, action_type, payload } = event as {
      approval_id: string
      action_type: string
      payload: Record<string, unknown>
    }
    const command = typeof payload?.command === 'string' ? payload.command : ''
    return [
      ...items,
      { kind: 'gate', id: nextId(), approvalId: approval_id, actionType: action_type, command },
    ]
  }

  return items
}
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `npm --prefix web test`
Expected: PASS, шесть тестов нормализации.

- [ ] **Step 5: Коммит**

```bash
git add web/src/model/
git commit -m "feat(web): нормализация ленты — история и живой поток к одному типу"
```

---

### Task 8: Компонент вызовов инструментов

**Files:**
- Create: `web/src/components/ToolCalls.tsx`, `web/src/components/ToolCalls.css`, `web/src/components/ToolCalls.test.tsx`

**Interfaces:**
- Consumes: `ThreadItem` вида `call` из `web/src/model/thread.ts` (задача 7).
- Produces: `<ToolCalls calls={Extract<ThreadItem, {kind:'call'}>[]} />`.

**Правила из спека, которые проверяются тестами:** одна строка одинаковой формы; MCP-сервер стоит перед именем без значка; справа стоит результат, а не «успешно»; упавший вызов раскрыт изначально.

- [ ] **Step 1: Написать падающий тест**

`web/src/components/ToolCalls.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import type { ThreadItem } from '../model/thread'
import { ToolCalls } from './ToolCalls'

type Call = Extract<ThreadItem, { kind: 'call' }>

const call = (over: Partial<Call> = {}): Call => ({
  kind: 'call',
  id: 'c1',
  server: null,
  name: 'write_file',
  arg: 'memory/index.py',
  result: '+58 −4',
  status: 'ok',
  ...over,
})

describe('вызовы инструментов', () => {
  it('показывает имя, аргумент и результат', () => {
    render(<ToolCalls calls={[call()]} />)
    expect(screen.getByText('write_file')).toBeInTheDocument()
    expect(screen.getByText('memory/index.py')).toBeInTheDocument()
    expect(screen.getByText('+58 −4')).toBeInTheDocument()
  })

  it('ставит имя MCP-сервера перед названием и не рисует значок', () => {
    render(<ToolCalls calls={[call({ server: 'github', name: 'list_issues', result: '2 задачи' })]} />)
    expect(screen.getByText('github')).toBeInTheDocument()
    expect(screen.getByText('list_issues')).toBeInTheDocument()
    expect(screen.queryByText(/MCP/i)).not.toBeInTheDocument()
  })

  it('не пишет «успешно» вместо результата', () => {
    render(<ToolCalls calls={[call()]} />)
    expect(screen.queryByText(/успешно/i)).not.toBeInTheDocument()
  })

  it('раскрывает упавший вызов сразу, а успешный — по нажатию', async () => {
    const { rerender } = render(<ToolCalls calls={[call()]} />)
    expect(screen.queryByTestId('call-detail')).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /write_file/ }))
    expect(screen.getByTestId('call-detail')).toBeInTheDocument()

    rerender(<ToolCalls calls={[call({ status: 'error', result: 'код 1' })]} />)
    expect(screen.getByTestId('call-detail')).toBeInTheDocument()
  })
})
```

Добавить `@testing-library/user-event` в `devDependencies` (`"^14.5.2"`) и переустановить: `npm --prefix web install`.

- [ ] **Step 2: Убедиться, что тест падает**

Run: `npm --prefix web test`
Expected: FAIL — не найден модуль `./ToolCalls`.

- [ ] **Step 3: Написать стили**

`web/src/components/ToolCalls.css`:

```css
/* Группа вызовов: общий фон, волосяные разделители, ни одной карточки. */
.calls {
  margin: 14px 0 6px;
  border-radius: 10px;
  overflow: hidden;
  background: #1e1d1a;
}

.call {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 12px;
  border: 0;
  background: none;
  text-align: left;
  font-family: var(--mono);
  font-size: 12.8px;
  color: var(--muted);
  cursor: pointer;
}

.call + .call,
.call-detail + .call {
  box-shadow: inset 0 1px 0 var(--line-soft);
}

.call__mark {
  flex: 0 0 13px;
  text-align: center;
  color: var(--faint);
}
.call__server {
  color: var(--faint);
}
.call__slash {
  color: #4a4640;
}
.call__name {
  color: #d8d1c6;
}
.call__arg {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.call__result {
  white-space: nowrap;
  color: var(--faint);
}

.call--run .call__mark,
.call--run .call__result {
  color: var(--ember);
}
.call--error .call__mark,
.call--error .call__result {
  color: var(--bad);
}

.call-detail {
  padding: 0 12px 12px 36px;
  background: #1b1a17;
}
.call-detail pre {
  margin: 10px 0 0;
  padding: 9px 12px;
  border-radius: 7px;
  background: #141311;
  font-family: var(--mono);
  font-size: 12.2px;
  line-height: 1.7;
  color: #d6cfc4;
  overflow-x: auto;
}

/* Узкий экран: две строки. Результат не обрезается ради аргумента. */
@media (max-width: 899px) {
  .call {
    flex-wrap: wrap;
    row-gap: 2px;
  }
  .call__arg {
    order: 5;
    flex: 1 0 100%;
    padding-left: 23px;
    font-size: 11.8px;
    color: var(--faint);
  }
  .call__name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
}
```

- [ ] **Step 4: Написать компонент**

`web/src/components/ToolCalls.tsx`:

```tsx
import { useState } from 'react'

import type { ThreadItem } from '../model/thread'
import './ToolCalls.css'

type Call = Extract<ThreadItem, { kind: 'call' }>

const MARK: Record<Call['status'], string> = { ok: '✓', run: '▸', error: '✕' }

function CallRow({ call }: { call: Call }) {
  // Упавший вызов раскрыт изначально: прятать причину остановки бессмысленно.
  const [open, setOpen] = useState(call.status === 'error')

  return (
    <>
      <button
        type="button"
        className={`call call--${call.status}`}
        aria-expanded={open}
        onClick={() => setOpen((was) => !was)}
      >
        <span className="call__mark" aria-hidden="true">
          {MARK[call.status]}
        </span>
        {call.server !== null && (
          <>
            <span className="call__server">{call.server}</span>
            <span className="call__slash" aria-hidden="true">
              /
            </span>
          </>
        )}
        <span className="call__name">{call.name}</span>
        <span className="call__arg">{call.arg}</span>
        <span className="call__result">{call.result}</span>
      </button>
      {open && (
        <div className="call-detail" data-testid="call-detail">
          <pre>{call.arg || '—'}</pre>
          <pre>{call.result || '—'}</pre>
        </div>
      )}
    </>
  )
}

export function ToolCalls({ calls }: { calls: Call[] }) {
  if (calls.length === 0) return null
  return (
    <div className="calls">
      {calls.map((call) => (
        <CallRow key={call.id} call={call} />
      ))}
    </div>
  )
}
```

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `npm --prefix web test`
Expected: PASS, четыре теста вызовов.

- [ ] **Step 6: Коммит**

```bash
git add web/src/components/ToolCalls.tsx web/src/components/ToolCalls.css web/src/components/ToolCalls.test.tsx web/package.json web/package-lock.json
git commit -m "feat(web): группа вызовов инструментов и MCP"
```

---

### Task 9: Гейт разрешения

**Files:**
- Create: `web/src/components/Gate.tsx`, `web/src/components/Gate.css`, `web/src/components/Gate.test.tsx`

**Interfaces:**
- Consumes: `ThreadItem` вида `gate` (задача 7); `Api.decideApproval` (задача 6).
- Produces: `<Gate gate={Extract<ThreadItem,{kind:'gate'}>} onDecide={(approved: boolean) => void} />`.

- [ ] **Step 1: Написать падающий тест**

`web/src/components/Gate.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Gate } from './Gate'

const gate = {
  kind: 'gate' as const,
  id: 'g1',
  approvalId: 'ap-1',
  actionType: 'run_shell',
  command: 'uv run pytest tests/memory/ -q',
}

describe('гейт разрешения', () => {
  it('показывает команду и правило', () => {
    render(<Gate gate={gate} onDecide={() => {}} />)
    expect(screen.getByText('uv run pytest tests/memory/ -q')).toBeInTheDocument()
    expect(screen.getByText(/run_shell/)).toBeInTheDocument()
  })

  it('сообщает решение наверх', async () => {
    const onDecide = vi.fn()
    render(<Gate gate={gate} onDecide={onDecide} />)

    await userEvent.click(screen.getByRole('button', { name: 'Разрешить' }))
    expect(onDecide).toHaveBeenCalledWith(true)

    await userEvent.click(screen.getByRole('button', { name: 'Отклонить' }))
    expect(onDecide).toHaveBeenCalledWith(false)
  })

  it('ставит «Разрешить» первой кнопкой', () => {
    render(<Gate gate={gate} onDecide={() => {}} />)
    expect(screen.getAllByRole('button')[0]).toHaveTextContent('Разрешить')
  })
})
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `npm --prefix web test`
Expected: FAIL — не найден модуль `./Gate`.

- [ ] **Step 3: Написать стили**

`web/src/components/Gate.css`:

```css
/* Единственное акцентное пятно в ленте. */
.gate {
  margin: 16px 0 4px;
  padding: 15px 17px;
  border-radius: 10px;
  background: rgba(210, 98, 44, 0.07);
  box-shadow: inset 0 0 0 1px rgba(210, 98, 44, 0.3);
}

.gate__head {
  margin-bottom: 11px;
  font-size: 13.5px;
  color: #e9a277;
}

.gate__cmd {
  margin: 0 0 13px;
  padding: 10px 13px;
  border-radius: 7px;
  background: #171613;
  font-family: var(--mono);
  font-size: 13px;
  color: #e2dcd2;
  overflow-x: auto;
}

.gate__row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 9px;
}

.gate__btn {
  padding: 7px 14px;
  border: 0;
  border-radius: 7px;
  background: var(--raised);
  color: var(--text);
  font-family: var(--sans);
  font-size: 13px;
  cursor: pointer;
}
.gate__btn--primary {
  background: var(--ember);
  color: #1a1210;
  font-weight: 600;
}
.gate__btn--ghost {
  background: none;
  color: var(--muted);
}

.gate__rule {
  margin-left: auto;
  font-family: var(--mono);
  font-size: 12px;
  color: var(--faint);
}

/* Узкий экран: кнопки в столбик, цель нажатия не меньше 44 px. */
@media (max-width: 899px) {
  .gate__row {
    flex-direction: column;
    align-items: stretch;
  }
  .gate__btn {
    min-height: 44px;
  }
  .gate__rule {
    margin: 4px auto 0;
  }
}
```

- [ ] **Step 4: Написать компонент**

`web/src/components/Gate.tsx`:

```tsx
import type { ThreadItem } from '../model/thread'
import './Gate.css'

type GateItem = Extract<ThreadItem, { kind: 'gate' }>

export function Gate({
  gate,
  onDecide,
}: {
  gate: GateItem
  onDecide: (approved: boolean) => void
}) {
  return (
    <div className="gate">
      <div className="gate__head">Команда не выполнится без вашего решения</div>
      <pre className="gate__cmd">{gate.command}</pre>
      <div className="gate__row">
        <button type="button" className="gate__btn gate__btn--primary" onClick={() => onDecide(true)}>
          Разрешить
        </button>
        <button type="button" className="gate__btn gate__btn--ghost" onClick={() => onDecide(false)}>
          Отклонить
        </button>
        {/* Правило, по которому агент остановился: решение принимается со знанием причины. */}
        <span className="gate__rule">{gate.actionType}</span>
      </div>
    </div>
  )
}
```

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `npm --prefix web test`
Expected: PASS, три теста гейта.

- [ ] **Step 6: Коммит**

```bash
git add web/src/components/Gate.tsx web/src/components/Gate.css web/src/components/Gate.test.tsx
git commit -m "feat(web): гейт разрешения в ленте"
```

---

### Task 10: Поле ввода

**Files:**
- Create: `web/src/components/Composer.tsx`, `web/src/components/Composer.css`, `web/src/components/Composer.test.tsx`

**Interfaces:**
- Produces: `<Composer onSend={(text: string) => void} autonomy={string} executor={string} model={string} />`.

**Из спека:** режимы стоят под строкой ввода; место под микрофон присутствует, кнопка выключена.

- [ ] **Step 1: Написать падающий тест**

`web/src/components/Composer.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Composer } from './Composer'

const props = { autonomy: 'под надзором', executor: 'нативный цикл', model: 'qwen3-coder' }

describe('поле ввода', () => {
  it('отправляет текст и очищает поле', async () => {
    const onSend = vi.fn()
    render(<Composer {...props} onSend={onSend} />)

    const field = screen.getByRole('textbox', { name: /написать/i })
    await userEvent.type(field, 'прогони тесты')
    await userEvent.click(screen.getByRole('button', { name: 'Отправить' }))

    expect(onSend).toHaveBeenCalledWith('прогони тесты')
    expect(field).toHaveValue('')
  })

  it('не отправляет пустое', async () => {
    const onSend = vi.fn()
    render(<Composer {...props} onSend={onSend} />)
    await userEvent.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(onSend).not.toHaveBeenCalled()
  })

  it('показывает режимы под строкой', () => {
    render(<Composer {...props} onSend={() => {}} />)
    expect(screen.getByText(/под надзором/)).toBeInTheDocument()
    expect(screen.getByText(/нативный цикл/)).toBeInTheDocument()
    expect(screen.getByText(/qwen3-coder/)).toBeInTheDocument()
  })

  it('держит место под микрофон выключенной кнопкой', () => {
    render(<Composer {...props} onSend={() => {}} />)
    const mic = screen.getByRole('button', { name: /голосовой ввод/i })
    expect(mic).toBeDisabled()
    expect(mic).toHaveAccessibleDescription(/появится позже/i)
  })
})
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `npm --prefix web test`
Expected: FAIL — не найден модуль `./Composer`.

- [ ] **Step 3: Написать стили**

`web/src/components/Composer.css`:

```css
.composer {
  padding: 12px 24px 16px;
}
.composer__inner {
  max-width: 700px;
  margin: 0 auto;
}
.composer__box {
  padding: 12px 14px 9px;
  border-radius: 12px;
  background: var(--surface);
  box-shadow: inset 0 0 0 1px var(--line);
}
.composer__field {
  width: 100%;
  border: 0;
  background: none;
  color: var(--text);
  font-family: var(--sans);
  font-size: 15px;
  resize: none;
}
.composer__field::placeholder {
  color: var(--faint);
}
.composer__field:focus {
  outline: none;
}
.composer__foot {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 11px;
  font-size: 12.5px;
  color: var(--muted);
}
.composer__icon {
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 9px;
  background: var(--raised);
  color: var(--muted);
  cursor: pointer;
}
.composer__icon:disabled {
  cursor: default;
  opacity: 0.75;
}
.composer__icon--send {
  margin-left: 0;
  background: var(--ember);
  color: #1a1210;
}
.composer__spacer {
  flex: 1;
}

@media (max-width: 899px) {
  .composer {
    padding: 10px 12px 14px;
  }
  .composer__icon {
    width: 44px;
    height: 44px;
  }
  .composer__foot {
    gap: 8px;
    font-size: 12px;
  }
}
```

- [ ] **Step 4: Написать компонент**

`web/src/components/Composer.tsx`:

```tsx
import { useState } from 'react'

import './Composer.css'

export function Composer({
  onSend,
  autonomy,
  executor,
  model,
}: {
  onSend: (text: string) => void
  autonomy: string
  executor: string
  model: string
}) {
  const [text, setText] = useState('')

  function send() {
    const trimmed = text.trim()
    if (!trimmed) return
    onSend(trimmed)
    setText('')
  }

  return (
    <div className="composer">
      <div className="composer__inner">
        <div className="composer__box">
          <textarea
            className="composer__field"
            aria-label="Написать Сварогу"
            placeholder="Написать Сварогу"
            rows={1}
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
          <div className="composer__foot">
            {/* Режимы стоят там, где на них смотрят перед отправкой. */}
            <span>{autonomy}</span>
            <span>{executor}</span>
            <span>{model}</span>
            <span className="composer__spacer" />
            {/* Место под голос занято сразу: включение не потребует переверстки. */}
            <button
              type="button"
              className="composer__icon"
              aria-label="Голосовой ввод"
              aria-describedby="mic-hint"
              disabled
            >
              ●
            </button>
            <span id="mic-hint" hidden>
              Голосовой ввод появится позже
            </span>
            <button
              type="button"
              className="composer__icon composer__icon--send"
              aria-label="Отправить"
              onClick={send}
            >
              ↑
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `npm --prefix web test`
Expected: PASS, четыре теста поля ввода.

- [ ] **Step 6: Коммит**

```bash
git add web/src/components/Composer.tsx web/src/components/Composer.css web/src/components/Composer.test.tsx
git commit -m "feat(web): поле ввода с режимами и местом под микрофон"
```

---

### Task 11: Навигатор и оболочка

**Files:**
- Create: `web/src/components/Nav.tsx`, `web/src/components/Nav.css`, `web/src/components/Shell.tsx`, `web/src/components/Shell.css`, `web/src/components/Shell.test.tsx`

**Interfaces:**
- Consumes: `SessionSummary` (задача 6).
- Produces: `<Nav sessions={SessionSummary[]} activeId={string | null} onPick={(id: string) => void} onNew={() => void} />`; `<Shell nav={ReactNode} bar={ReactNode}>{children}</Shell>` с внутренним состоянием выдвижной панели и кнопкой её открытия.

**Из спека:** шкала накала — полоса 2 px слева от сессии, цвет по свежести; на узком экране навигатор выдвижной.

- [ ] **Step 1: Написать падающий тест**

`web/src/components/Shell.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { SessionSummary } from '../api/types'
import { Nav } from './Nav'
import { Shell } from './Shell'

const session = (over: Partial<SessionSummary>): SessionSummary => ({
  session_id: 's1',
  title: 'FTS-поиск по памяти',
  workspace: null,
  updated_at: new Date().toISOString(),
  runs_count: 1,
  last_state: 'succeeded',
  ...over,
})

describe('навигатор', () => {
  it('показывает сессии и разделы', () => {
    render(<Nav sessions={[session({})]} activeId="s1" onPick={() => {}} onNew={() => {}} />)
    expect(screen.getByText('FTS-поиск по памяти')).toBeInTheDocument()
    expect(screen.getByText('Скиллы')).toBeInTheDocument()
    expect(screen.getByText('Память')).toBeInTheDocument()
    expect(screen.getByText('Настройки')).toBeInTheDocument()
  })

  it('сообщает выбор сессии', async () => {
    const onPick = vi.fn()
    render(<Nav sessions={[session({})]} activeId={null} onPick={onPick} onNew={() => {}} />)
    await userEvent.click(screen.getByRole('button', { name: /FTS-поиск/ }))
    expect(onPick).toHaveBeenCalledWith('s1')
  })

  it('красит шкалу накала по свежести', () => {
    const day = 24 * 60 * 60 * 1000
    render(
      <Nav
        sessions={[
          session({ session_id: 'fresh', title: 'свежая', updated_at: new Date().toISOString() }),
          session({
            session_id: 'old',
            title: 'старая',
            updated_at: new Date(Date.now() - 8 * day).toISOString(),
          }),
        ]}
        activeId={null}
        onPick={() => {}}
        onNew={() => {}}
      />,
    )
    expect(screen.getByTestId('heat-fresh')).toHaveAttribute('data-heat', '1')
    expect(screen.getByTestId('heat-old')).toHaveAttribute('data-heat', '4')
  })
})

describe('оболочка', () => {
  it('открывает и закрывает выдвижной навигатор', async () => {
    render(
      <Shell nav={<div>навигатор</div>} bar={<div>шапка</div>}>
        <div>лента</div>
      </Shell>,
    )

    expect(screen.getByTestId('shell-nav')).toHaveAttribute('data-open', 'false')
    await userEvent.click(screen.getByRole('button', { name: /показать навигатор/i }))
    expect(screen.getByTestId('shell-nav')).toHaveAttribute('data-open', 'true')
    await userEvent.click(screen.getByTestId('shell-scrim'))
    expect(screen.getByTestId('shell-nav')).toHaveAttribute('data-open', 'false')
  })
})
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `npm --prefix web test`
Expected: FAIL — не найдены модули `./Nav` и `./Shell`.

- [ ] **Step 3: Написать `Nav`**

`web/src/components/Nav.css`:

```css
.nav {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--surface);
}
.nav__top {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 14px 10px;
  font-size: 15px;
  font-weight: 650;
}
.nav__new,
.nav__item,
.nav__section {
  display: flex;
  align-items: center;
  gap: 10px;
  width: calc(100% - 20px);
  margin: 0 10px;
  padding: 7px 12px;
  border: 0;
  border-radius: 7px;
  background: none;
  color: #c6bfb4;
  font-family: var(--sans);
  font-size: 13.5px;
  text-align: left;
  cursor: pointer;
}
.nav__new {
  margin-bottom: 10px;
  background: var(--raised);
  color: var(--text);
}
.nav__item--active {
  background: var(--raised);
  color: var(--text);
}
.nav__list {
  flex: 1;
  overflow-y: auto;
}
.nav__day {
  padding: 12px 22px 5px;
  font-size: 11.5px;
  color: var(--faint);
}
.nav__title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.nav__foot {
  padding: 8px 0 12px;
  border-top: 1px solid var(--line-soft);
}
.nav__count {
  margin-left: auto;
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--faint);
}

/* Шкала накала: свежесть и состояние одним каналом. */
.heat {
  flex: 0 0 2px;
  width: 2px;
  height: 15px;
  border-radius: 1px;
}
.heat[data-heat='0'] {
  background: #ffe7be;
  box-shadow: 0 0 7px rgba(255, 210, 140, 0.6);
}
.heat[data-heat='1'] {
  background: var(--ember);
}
.heat[data-heat='2'] {
  background: #a24b21;
}
.heat[data-heat='3'] {
  background: #6b3a22;
}
.heat[data-heat='4'] {
  background: #423a34;
}

@media (max-width: 899px) {
  .nav__item,
  .nav__new,
  .nav__section {
    min-height: 44px;
  }
}
```

`web/src/components/Nav.tsx`:

```tsx
import type { SessionSummary } from '../api/types'
import './Nav.css'

const HOUR = 60 * 60 * 1000
const DAY = 24 * HOUR

/** 0 — идёт сейчас, дальше остывает до 4 (архив). */
export function heatLevel(session: SessionSummary, now: number = Date.now()): number {
  if (session.last_state === 'running') return 0
  const age = now - Date.parse(session.updated_at)
  if (age < HOUR) return 1
  if (age < DAY) return 2
  if (age < 7 * DAY) return 3
  return 4
}

function dayLabel(session: SessionSummary, now: number = Date.now()): string {
  const age = now - Date.parse(session.updated_at)
  if (age < DAY) return 'Сегодня'
  if (age < 2 * DAY) return 'Вчера'
  if (age < 7 * DAY) return 'Прошлая неделя'
  return 'Ранее'
}

export function Nav({
  sessions,
  activeId,
  onPick,
  onNew,
}: {
  sessions: SessionSummary[]
  activeId: string | null
  onPick: (sessionId: string) => void
  onNew: () => void
}) {
  let lastLabel = ''

  return (
    <nav className="nav">
      <div className="nav__top">Сварог</div>
      <button type="button" className="nav__new" onClick={onNew}>
        ＋ Новый чат
      </button>

      <div className="nav__list">
        {sessions.map((session) => {
          const label = dayLabel(session)
          const header = label === lastLabel ? null : <div className="nav__day">{label}</div>
          lastLabel = label
          return (
            <div key={session.session_id}>
              {header}
              <button
                type="button"
                className={`nav__item${session.session_id === activeId ? ' nav__item--active' : ''}`}
                onClick={() => onPick(session.session_id)}
              >
                <span
                  className="heat"
                  data-testid={`heat-${session.session_id}`}
                  data-heat={heatLevel(session)}
                />
                <span className="nav__title">{session.title}</span>
              </button>
            </div>
          )
        })}
      </div>

      <div className="nav__foot">
        <button type="button" className="nav__section">
          Скиллы
        </button>
        <button type="button" className="nav__section">
          Память
        </button>
        <button type="button" className="nav__section">
          Настройки
        </button>
      </div>
    </nav>
  )
}
```

- [ ] **Step 4: Написать `Shell`**

`web/src/components/Shell.css`:

```css
.shell {
  display: flex;
  height: 100%;
}

.shell__nav {
  flex: 0 0 246px;
  width: 246px;
}

.shell__main {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
}

.shell__bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 24px;
  border-bottom: 1px solid var(--line-soft);
  font-size: 12.5px;
  color: var(--muted);
}

.shell__burger {
  display: none;
}

.shell__scrim {
  display: none;
}

/* Ниже 900 px навигатор выдвижной, содержимое — во всю ширину. */
@media (max-width: 899px) {
  .shell__nav {
    position: fixed;
    z-index: 5;
    top: 0;
    bottom: 0;
    left: 0;
    width: 274px;
    flex: none;
    transform: translateX(-100%);
    transition: transform 180ms ease;
    box-shadow: 14px 0 40px rgba(0, 0, 0, 0.5);
  }
  .shell__nav[data-open='true'] {
    transform: translateX(0);
  }
  .shell__nav[data-open='true'] ~ .shell__scrim {
    display: block;
    position: fixed;
    z-index: 4;
    inset: 0;
    background: rgba(10, 9, 8, 0.62);
  }
  .shell__burger {
    display: grid;
    place-items: center;
    width: 44px;
    height: 44px;
    margin-left: -10px;
    border: 0;
    border-radius: 9px;
    background: none;
    color: var(--muted);
    cursor: pointer;
  }
  .shell__bar {
    padding: 6px 12px;
  }
}
```

`web/src/components/Shell.tsx`:

```tsx
import { type ReactNode, useState } from 'react'

import './Shell.css'

export function Shell({
  nav,
  bar,
  children,
}: {
  nav: ReactNode
  bar: ReactNode
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)

  return (
    <div className="shell">
      <div className="shell__nav" data-testid="shell-nav" data-open={open}>
        {nav}
      </div>
      <div
        className="shell__scrim"
        data-testid="shell-scrim"
        onClick={() => setOpen(false)}
        aria-hidden="true"
      />
      <div className="shell__main">
        <div className="shell__bar">
          <button
            type="button"
            className="shell__burger"
            aria-label="Показать навигатор"
            onClick={() => setOpen((was) => !was)}
          >
            ☰
          </button>
          {bar}
        </div>
        {children}
      </div>
    </div>
  )
}
```

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `npm --prefix web test`
Expected: PASS, четыре теста навигатора и оболочки.

- [ ] **Step 6: Коммит**

```bash
git add web/src/components/Nav.tsx web/src/components/Nav.css web/src/components/Shell.tsx web/src/components/Shell.css web/src/components/Shell.test.tsx
git commit -m "feat(web): навигатор со шкалой накала и адаптивная оболочка"
```

---

### Task 12: Экран диалога

**Files:**
- Create: `web/src/api/stream.ts`, `web/src/screens/ChatScreen.tsx`, `web/src/screens/ChatScreen.css`, `web/src/screens/ChatScreen.test.tsx`
- Modify: `web/src/App.tsx`

**Interfaces:**
- Consumes: `Api` (задача 6), `fromHistory`/`applyEvent`/`ThreadItem` (задача 7), `ToolCalls` (8), `Gate` (9), `Composer` (10), `Nav`/`Shell` (11).
- Produces: `subscribeRun(baseUrl: string, runId: string, token: string | undefined, onEvent: (event: StreamEvent) => void): () => void`; `<ChatScreen api={Api} />`.

- [ ] **Step 1: Написать падающий тест**

`web/src/screens/ChatScreen.test.tsx`:

```tsx
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { Api } from '../api/client'
import { ChatScreen } from './ChatScreen'

function fakeApi(over: Partial<Api> = {}): Api {
  return {
    listSessions: vi.fn().mockResolvedValue([
      {
        session_id: 's1',
        title: 'FTS-поиск по памяти',
        workspace: null,
        updated_at: new Date().toISOString(),
        runs_count: 1,
        last_state: 'succeeded',
      },
    ]),
    sessionThread: vi.fn().mockResolvedValue({
      session_id: 's1',
      title: 'FTS-поиск по памяти',
      items: [
        { kind: 'user', text: 'Добавь FTS-поиск', server: null, name: '', arg: '', result: '', status: '' },
        {
          kind: 'call',
          text: '',
          server: null,
          name: 'write_file',
          arg: 'memory/index.py',
          result: '+58 −4',
          status: 'succeeded',
        },
      ],
    }),
    createSession: vi.fn().mockResolvedValue({ session_id: 's2' }),
    sendMessage: vi.fn().mockResolvedValue({ run_id: 'r1', state: 'running' }),
    decideApproval: vi.fn().mockResolvedValue({ run_id: 'r1', state: 'running' }),
    ...over,
  }
}

describe('экран диалога', () => {
  it('рисует историю выбранной сессии', async () => {
    render(<ChatScreen api={fakeApi()} />)

    await waitFor(() => expect(screen.getByText('Добавь FTS-поиск')).toBeInTheDocument())
    expect(screen.getByText('write_file')).toBeInTheDocument()
    expect(screen.getByText('+58 −4')).toBeInTheDocument()
  })

  it('отправляет сообщение в текущую сессию', async () => {
    const api = fakeApi()
    render(<ChatScreen api={api} />)
    await waitFor(() => expect(screen.getByText('Добавь FTS-поиск')).toBeInTheDocument())

    await userEvent.type(screen.getByRole('textbox', { name: /написать/i }), 'прогони тесты')
    await userEvent.click(screen.getByRole('button', { name: 'Отправить' }))

    expect(api.sendMessage).toHaveBeenCalledWith('s1', 'прогони тесты')
    await waitFor(() => expect(screen.getByText('прогони тесты')).toBeInTheDocument())
  })

  it('показывает ошибку загрузки, а не пустой экран', async () => {
    const api = fakeApi({ listSessions: vi.fn().mockRejectedValue(new Error('нет связи')) })
    render(<ChatScreen api={api} />)
    await waitFor(() =>
      expect(screen.getByText(/не удалось загрузить сессии/i)).toBeInTheDocument(),
    )
  })

  it('пока грузится — говорит об этом, а не показывает пустоту', () => {
    render(<ChatScreen api={fakeApi()} />)
    expect(screen.getByText(/загружаем/i)).toBeInTheDocument()
  })

  it('пустая сессия приглашает к действию, а не сообщает «нет данных»', async () => {
    const api = fakeApi({
      sessionThread: vi
        .fn()
        .mockResolvedValue({ session_id: 's1', title: 'Новый чат', items: [] }),
    })
    render(<ChatScreen api={api} />)

    await waitFor(() => expect(screen.getByText(/поставьте задачу/i)).toBeInTheDocument())
    expect(screen.queryByText(/нет данных/i)).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `npm --prefix web test`
Expected: FAIL — не найден модуль `./ChatScreen`.

- [ ] **Step 3: Написать подписку на поток**

`web/src/api/stream.ts`:

```ts
import type { StreamEvent } from '../model/thread'

/**
 * Подписка на события run'а. Возвращает функцию отписки.
 *
 * Токен передаётся query-параметром: WebSocket в браузере не позволяет
 * задать заголовок Authorization, и gateway это уже учитывает
 * (`websocket.query_params.get("token")`).
 */
export function subscribeRun(
  baseUrl: string,
  runId: string,
  token: string | undefined,
  onEvent: (event: StreamEvent) => void,
): () => void {
  const base = baseUrl || window.location.origin
  const url = new URL(`/runs/${encodeURIComponent(runId)}/events`, base)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  if (token) url.searchParams.set('token', token)

  const socket = new WebSocket(url)
  socket.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data as string) as StreamEvent)
    } catch {
      // Битое событие пропускаем: одна плохая строка не должна валить ленту.
    }
  }
  return () => socket.close()
}
```

- [ ] **Step 4: Написать стили экрана**

`web/src/screens/ChatScreen.css`:

```css
.chat {
  display: flex;
  flex: 1;
  min-height: 0;
  flex-direction: column;
}

/* Лента прижата к низу: свежее у поля ввода. */
.chat__thread {
  display: flex;
  flex: 1;
  flex-direction: column;
  justify-content: flex-end;
  padding: 26px 0 10px;
  overflow-y: auto;
}

.chat__col {
  width: 100%;
  max-width: 700px;
  margin: 0 auto;
  padding: 0 24px;
}

.chat__you {
  margin-bottom: 26px;
  padding: 12px 16px;
  border-radius: 12px;
  background: var(--raised);
  font-size: 15px;
}

.chat__say {
  margin-bottom: 26px;
  font-size: 15.2px;
  line-height: 1.68;
  color: #e2dcd2;
}

.chat__error {
  margin: 26px 0;
  color: var(--bad);
}

.chat__hint {
  margin: 26px 0;
  color: var(--muted);
}

@media (max-width: 899px) {
  .chat__col {
    padding: 0 14px;
  }
}
```

- [ ] **Step 5: Написать экран**

`web/src/screens/ChatScreen.tsx`:

```tsx
import { useCallback, useEffect, useRef, useState } from 'react'

import type { Api } from '../api/client'
import { subscribeRun } from '../api/stream'
import type { SessionSummary } from '../api/types'
import { Composer } from '../components/Composer'
import { Gate } from '../components/Gate'
import { Nav } from '../components/Nav'
import { Shell } from '../components/Shell'
import { ToolCalls } from '../components/ToolCalls'
import { applyEvent, fromHistory, type ThreadItem } from '../model/thread'
import './ChatScreen.css'

type Call = Extract<ThreadItem, { kind: 'call' }>

/** Подряд идущие вызовы рисуются одной группой, а не по карточке на каждый. */
function groupItems(items: ThreadItem[]): (ThreadItem | { kind: 'calls'; id: string; calls: Call[] })[] {
  const grouped: (ThreadItem | { kind: 'calls'; id: string; calls: Call[] })[] = []
  for (const item of items) {
    const last = grouped[grouped.length - 1]
    if (item.kind === 'call') {
      if (last && last.kind === 'calls') {
        last.calls.push(item)
        continue
      }
      grouped.push({ kind: 'calls', id: `g-${item.id}`, calls: [item] })
      continue
    }
    grouped.push(item)
  }
  return grouped
}

export function ChatScreen({ api, baseUrl = '', token }: { api: Api; baseUrl?: string; token?: string }) {
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [items, setItems] = useState<ThreadItem[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const unsubscribe = useRef<(() => void) | null>(null)

  useEffect(() => {
    api
      .listSessions()
      .then((listed) => {
        setSessions(listed)
        setActiveId((current) => current ?? listed[0]?.session_id ?? null)
      })
      .catch(() => setError('Не удалось загрузить сессии. Проверьте, что svarog serve запущен.'))
      .finally(() => setLoading(false))
  }, [api])

  useEffect(() => {
    if (activeId === null) return
    api
      .sessionThread(activeId)
      .then((thread) => setItems(fromHistory(thread.items)))
      .catch(() => setError('Не удалось загрузить историю этой сессии.'))
  }, [api, activeId])

  useEffect(() => () => unsubscribe.current?.(), [])

  const send = useCallback(
    async (text: string) => {
      if (activeId === null) return
      setItems((current) => [...current, { kind: 'user', id: `u-${Date.now()}`, text }])
      const ref = await api.sendMessage(activeId, text)
      unsubscribe.current?.()
      unsubscribe.current = subscribeRun(baseUrl, ref.run_id, token, (event) =>
        setItems((current) => applyEvent(current, event)),
      )
    },
    [api, activeId, baseUrl, token],
  )

  const decide = useCallback(
    async (approvalId: string, approved: boolean) => {
      await api.decideApproval(approvalId, approved)
      setItems((current) => current.filter((item) => !(item.kind === 'gate' && item.approvalId === approvalId)))
    },
    [api],
  )

  const active = sessions.find((session) => session.session_id === activeId)

  return (
    <Shell
      nav={
        <Nav
          sessions={sessions}
          activeId={activeId}
          onPick={setActiveId}
          onNew={async () => {
            const created = await api.createSession('Новый чат')
            setActiveId(created.session_id)
            setItems([])
            setSessions(await api.listSessions())
          }}
        />
      }
      bar={<span>{active?.title ?? 'Сварог'}</span>}
    >
      <div className="chat">
        <div className="chat__thread">
          <div className="chat__col">
            {error !== null && <p className="chat__error">{error}</p>}
            {error === null && loading && <p className="chat__hint">Загружаем сессии…</p>}
            {/* Пустой экран — приглашение к действию, а не «нет данных». */}
            {error === null && !loading && items.length === 0 && (
              <p className="chat__hint">
                Поставьте задачу — Сварог заведёт ветку и покажет каждый свой шаг.
              </p>
            )}
            {groupItems(items).map((entry) => {
              if (entry.kind === 'calls') return <ToolCalls key={entry.id} calls={entry.calls} />
              if (entry.kind === 'user')
                return (
                  <div key={entry.id} className="chat__you">
                    {entry.text}
                  </div>
                )
              if (entry.kind === 'say')
                return (
                  <div key={entry.id} className="chat__say">
                    {entry.text}
                  </div>
                )
              if (entry.kind === 'gate')
                return (
                  <Gate
                    key={entry.id}
                    gate={entry}
                    onDecide={(approved) => void decide(entry.approvalId, approved)}
                  />
                )
              return null
            })}
          </div>
        </div>
        <Composer
          onSend={(text) => void send(text)}
          autonomy="под надзором"
          executor="нативный цикл"
          model="qwen3-coder"
        />
      </div>
    </Shell>
  )
}
```

- [ ] **Step 6: Подключить экран в `App`**

`web/src/App.tsx`:

```tsx
import { createClient } from './api/client'
import { ChatScreen } from './screens/ChatScreen'

// Статика раздаётся тем же svarog serve, поэтому базовый URL пустой.
const api = createClient({ baseUrl: '' })

export function App() {
  return <ChatScreen api={api} />
}
```

- [ ] **Step 7: Убедиться, что тесты проходят**

Run: `npm --prefix web test && npm --prefix web run build`
Expected: PASS, три теста экрана; сборка успешна.

- [ ] **Step 8: Коммит**

```bash
git add web/src/api/stream.ts web/src/screens/ web/src/App.tsx
git commit -m "feat(web): экран диалога — история, отправка, живой поток"
```

---

### Task 13: Раздача статики из `svarog serve` и CORS

**Files:**
- Create: `src/svarog_harness/gateway/static.py`
- Modify: `src/svarog_harness/gateway/api.py`
- Modify: `pyproject.toml` (сборка бандла в wheel)
- Modify: `.github/workflows/ci.yml` (порядок шагов: сборка клиента до сборки пакета)
- Test: `tests/test_gateway_web.py`

**Interfaces:**
- Consumes: `create_app` из `api.py`.
- Produces: `web_dist_dir() -> Path | None`; `GET /` отдаёт `index.html`, если бандл собран, и 404 с внятным текстом, если нет; CORS включён только когда задан `GORN_CORS_ORIGINS`.

- [ ] **Step 1: Написать падающий тест**

```python
def test_root_explains_when_bundle_missing(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))
    response = client.get("/")
    assert response.status_code == 404
    assert "npm --prefix web run build" in response.json()["detail"]


def test_root_serves_bundle_when_present(
    service: GatewayService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Сварог</title>", encoding="utf-8")
    monkeypatch.setenv("SVAROG_WEB_DIST", str(dist))

    client = TestClient(create_app(service=service))
    response = client.get("/")

    assert response.status_code == 200
    assert "Сварог" in response.text


def test_cors_disabled_by_default(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))
    response = client.get("/healthz", headers={"Origin": "http://localhost:5173"})
    assert "access-control-allow-origin" not in response.headers
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `uv run pytest tests/test_gateway_web.py -k "root_ or cors" -v`
Expected: FAIL — `GET /` возвращает 404 без поля `detail` с подсказкой.

- [ ] **Step 3: Написать поиск бандла**

`src/svarog_harness/gateway/static.py`:

```python
"""Поиск собранного клиента и его раздача из gateway.

Бандл едет внутри пакета (собирается в CI), но при разработке лежит в
`web/dist` рядом с исходниками — ищем оба места, чтобы `svarog serve`
поднимал интерфейс и из чекаута, и из установленного колеса.
"""

import os
from pathlib import Path


def web_dist_dir() -> Path | None:
    """Каталог собранного клиента или None, если бандла нет."""
    override = os.environ.get("SVAROG_WEB_DIST")
    if override:
        candidate = Path(override)
        return candidate if (candidate / "index.html").is_file() else None

    packaged = Path(__file__).resolve().parent / "web"
    if (packaged / "index.html").is_file():
        return packaged

    # Чекаут репозитория: src/svarog_harness/gateway → корень → web/dist
    checkout = Path(__file__).resolve().parents[3] / "web" / "dist"
    return checkout if (checkout / "index.html").is_file() else None
```

- [ ] **Step 4: Подключить раздачу и CORS**

В `src/svarog_harness/gateway/api.py`, сразу после `app = FastAPI(...)`:

```python
    # CORS нужен только режиму раздельной разработки: в бою статика едет
    # с того же origin, что и API.
    origins = [o for o in os.environ.get("GORN_CORS_ORIGINS", "").split(",") if o]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
```

В конец `create_app`, перед `return app`:

```python
    dist = web_dist_dir()
    if dist is not None:
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(dist / "index.html")

    else:

        @app.get("/", include_in_schema=False)
        async def index_missing() -> None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "Клиент не собран. Соберите его: npm --prefix web ci "
                    "&& npm --prefix web run build"
                ),
            )

    return app
```

Импорты вверху файла: `import os`, `from fastapi.middleware.cors import CORSMiddleware`, `from fastapi.staticfiles import StaticFiles`, `from svarog_harness.gateway.static import web_dist_dir`. `FileResponse` и `HTTPException` уже импортированы.

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `uv run pytest tests/test_gateway_web.py -v`
Expected: PASS, все тесты файла.

- [ ] **Step 6: Класть бандл в колесо**

В `pyproject.toml`, в секцию сборки:

```toml
[tool.hatch.build.targets.wheel.force-include]
# Бандл клиента собирается до сборки пакета (шаг CI) и едет внутри колеса,
# чтобы запуск Сварога не требовал Node.
"web/dist" = "svarog_harness/gateway/web"
```

- [ ] **Step 7: Проверить порядок шагов CI**

В `.github/workflows/ci.yml` шаг «Web build» должен стоять до любого шага, который собирает пакет. Если сборки пакета в CI нет, менять ничего не нужно — проверить командой:

Run: `grep -n "build\|hatch\|wheel" .github/workflows/ci.yml`

- [ ] **Step 8: Проверить руками**

```bash
npm --prefix web ci && npm --prefix web run build
uv run svarog serve
```

Открыть `http://127.0.0.1:8000/` и убедиться: навигатор виден, сессии загружаются, сообщение отправляется, гейт появляется. Затем сузить окно до 375 px и проверить, что навигатор стал выдвижным, кнопки гейта встали в столбик, горизонтальной прокрутки страницы нет.

- [ ] **Step 9: Прогнать полный набор проверок**

Run: `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest && npm --prefix web test`
Expected: всё зелёное.

- [ ] **Step 10: Коммит**

```bash
git add src/svarog_harness/gateway/static.py src/svarog_harness/gateway/api.py pyproject.toml tests/test_gateway_web.py .github/workflows/ci.yml
git commit -m "feat(gateway): раздача собранного клиента и CORS для разработки"
```

---

## Что этот план не закрывает

- **Экраны «Настройки» и «Память»** — планы 2 и 3, пишутся после выполнения этого.
- **Скиллы, трейсы, очередь approvals, тенанты** — за скоупом спека.
- **Голосовой ввод и ответ** — здесь только место в разметке и выключенная кнопка; четыре решения записаны в спеке.
- **Структурированный итог инструментов.** Чтобы справа от вызова стояло `+58 −4`, `3 записи`, `код 1`, как нарисовано в макете, инструменты должны сообщать измеримый результат, а не прозу. Это поле `meta` в `ToolResult` и его заполнение в `write_file`, `edit_file`, `search_memory`, `run_shell` — работа в `tools/` и `runtime/`, трогающая контракт инструментов и их тесты. Отдельный план; форма строки вызова при этом не меняется, меняется только содержимое правого поля, поэтому переверстки не потребуется.
- **Переключение режимов из поля ввода** — значения показываются, но пока не редактируются: смена автономии требует передачи `autonomy` в `POST /sessions/{id}/messages`, что делается вместе с экраном настроек в плане 2.
