# Автогенерация названий чатов — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** После завершения первого run'а сессии gateway генерирует короткое название чата aux-моделью (fallback — обрезка первого сообщения) и пишет его в `sessions.title`; UI подхватывает через существующий поллинг.

**Architecture:** Новый модуль чистых функций `gateway/autotitle.py` (промпт, чистка, fallback, условие срабатывания) + фоновый хук `_autotitle_bg` в `GatewayService`, запускаемый из `_run_bg`/`_resume_bg` после `_publish_finished`. Фронтенд не меняется.

**Tech Stack:** Python 3.12, SQLAlchemy async (SQLite), существующий `OpenAICompatibleProvider` через `auxiliary_provider()`, pytest (asyncio_mode=auto).

**Спека:** `docs/superpowers/specs/2026-08-04-chat-autotitle-design.md`

## Global Constraints

- Комментарии и docstrings — по-русски, в стиле кодовой базы (см. `memory/autocapture.py`).
- Перед каждым коммитом: `uv run ruff check src tests && uv run ruff format src tests`, `uv run mypy`, целевые тесты зелёные (AGENTS.md п.3).
- Дефолтные названия ровно: `""`, `"Новый чат"`, `"gateway-сессия"` (спека).
- Флаг `meta["autotitle"]` (`"done"` | `"fallback"`) окончательный — повторных генераций нет.
- Лимиты: название ≤ 200 символов; вход модели: вопрос ≤ 2000, ответ ≤ 1000; fallback ≤ 60 символов по границе слова.
- Любая ошибка генерации не должна влиять на run/сессию (best-effort, по образцу `memory/autocapture.py`).
- `web/` не трогаем вообще.

---

### Task 1: Модуль `gateway/autotitle.py` (чистые функции + юнит-тесты)

**Files:**
- Create: `src/svarog_harness/gateway/autotitle.py`
- Test: `tests/test_autotitle.py`

**Interfaces:**
- Consumes: `ChatMessage`, `ModelProvider` из `svarog_harness.llm.provider`.
- Produces (Task 2 полагается на эти сигнатуры):
  - `DEFAULT_TITLES: frozenset[str]`
  - `needs_autotitle(title: str | None, meta: dict | None) -> bool`
  - `clean_title(raw: str) -> str | None`
  - `fallback_title(task: str) -> str | None`
  - `async generate_title(provider: ModelProvider, task: str, answer: str) -> str | None`
  - `async title_for(provider_factory: Callable[[], ModelProvider], task: str, answer: str) -> str | None` — обёртка: строит провайдера (ошибка построения → warning в лог + `None`) и зовёт `generate_title`.

- [ ] **Step 1: Написать падающие юнит-тесты**

Создать `tests/test_autotitle.py`:

```python
"""Юнит-тесты автоназвания чатов: чистка ответа модели и fallback (спека 2026-08-04)."""

from collections.abc import Callable

from svarog_harness.gateway.autotitle import (
    clean_title,
    fallback_title,
    generate_title,
    needs_autotitle,
    title_for,
)
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolDefinition,
    Usage,
)


class OneShotProvider(ModelProvider):
    def __init__(self, content: str = "", *, error: bool = False) -> None:
        self.content = content
        self.error = error
        self.calls: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.calls.append(list(messages))
        if self.error:
            raise RuntimeError("aux недоступна")
        return CompletionResult(content=self.content, usage=Usage(1, 1))


def test_clean_title_strips_quotes_period_and_newlines() -> None:
    assert clean_title("«Настройка  CI»\n") == "Настройка CI"
    assert clean_title('"Deploy pipeline."') == "Deploy pipeline"
    assert clean_title("Один\nдва   три") == "Один два три"


def test_clean_title_garbage_is_none() -> None:
    assert clean_title("  \n") is None
    assert clean_title("«».") is None


def test_clean_title_cuts_to_200() -> None:
    cut = clean_title("х" * 500)
    assert cut is not None and len(cut) == 200


def test_fallback_title_cuts_on_word_boundary() -> None:
    text = "напиши длинное сочинение про кота который жил на крыше дома и ловил голубей"
    result = fallback_title(text)
    assert result is not None
    assert result.endswith("…") and len(result) <= 61
    assert not result[:-1].endswith(" ")


def test_fallback_title_short_text_kept_as_is() -> None:
    assert fallback_title("почини баг") == "почини баг"
    assert fallback_title("   ") is None


def test_needs_autotitle_only_for_default_titles_without_flag() -> None:
    assert needs_autotitle("Новый чат", None)
    assert needs_autotitle("gateway-сессия", {})
    assert needs_autotitle("", {})
    assert needs_autotitle(None, {})
    assert not needs_autotitle("Мой чат", {})
    assert not needs_autotitle("Новый чат", {"autotitle": "done"})
    assert not needs_autotitle("Новый чат", {"autotitle": "fallback"})


async def test_generate_title_happy_path() -> None:
    provider = OneShotProvider("«География Франции.»")
    title = await generate_title(provider, "Какая столица Франции?", "Париж")
    assert title == "География Франции"
    body = provider.calls[0][1].content
    assert "Какая столица Франции?" in body
    assert "Париж" in body


async def test_generate_title_error_returns_none() -> None:
    provider = OneShotProvider(error=True)
    assert await generate_title(provider, "вопрос", "ответ") is None


async def test_generate_title_without_answer_omits_answer_block() -> None:
    provider = OneShotProvider("Название")
    assert await generate_title(provider, "вопрос", "") == "Название"
    assert "Ответ:" not in provider.calls[0][1].content


async def test_generate_title_truncates_long_input() -> None:
    provider = OneShotProvider("Название")
    await generate_title(provider, "в" * 5000, "о" * 5000)
    body = provider.calls[0][1].content
    assert len(body) < 3200  # 2000 (вопрос) + 1000 (ответ) + разметка


async def test_title_for_factory_error_returns_none() -> None:
    def broken_factory() -> ModelProvider:
        raise RuntimeError("нет aux-провайдера в конфиге")

    assert await title_for(broken_factory, "вопрос", "ответ") is None


async def test_title_for_happy_path() -> None:
    provider = OneShotProvider("Название")
    assert await title_for(lambda: provider, "вопрос", "ответ") == "Название"
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_autotitle.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.gateway.autotitle'`

- [ ] **Step 3: Реализовать модуль**

Создать `src/svarog_harness/gateway/autotitle.py`:

```python
"""Автогенерация названия чата по первому обмену (спека 2026-08-04).

Один best-effort вызов aux-модели по образцу memory/autocapture.py: любой
сбой модели или её сборки -> None, решение о fallback принимает вызывающий
(GatewayService._autotitle_bg). Исключения наружу не выходят.
"""

import logging
from collections.abc import Callable

from svarog_harness.llm.provider import ChatMessage, ModelProvider

logger = logging.getLogger(__name__)

# «Безымянные» названия: хардкод клиента (web/src/App.tsx) и серверный
# дефолт (GatewayService.create_session). Только такие чаты переименовываем.
DEFAULT_TITLES = frozenset({"", "Новый чат", "gateway-сессия"})

_TITLE_MAX = 200  # лимит recorder'а: create_session/rename режут title[:200]
_FALLBACK_MAX = 60
_TASK_LIMIT = 2000
_ANSWER_LIMIT = 1000
_QUOTES = "\"'«»“”‘’`"

_SYSTEM = (
    "Придумай короткое название диалогу: 3-6 слов, на языке диалога, "
    "без кавычек, без точки в конце. Верни ТОЛЬКО название, без пояснений."
)


def needs_autotitle(title: str | None, meta: dict | None) -> bool:
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
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest tests/test_autotitle.py -v`
Expected: PASS (12 тестов)

- [ ] **Step 5: Линт, типы, коммит**

```bash
uv run ruff check src/svarog_harness/gateway/autotitle.py tests/test_autotitle.py && uv run ruff format src/svarog_harness/gateway/autotitle.py tests/test_autotitle.py && uv run mypy src/svarog_harness/gateway/autotitle.py
git add src/svarog_harness/gateway/autotitle.py tests/test_autotitle.py
git commit -m "feat(gateway): модуль автоназвания чатов — промпт, чистка, fallback"
```

---

### Task 2: Хук в `GatewayService` + сервисные тесты

**Files:**
- Modify: `src/svarog_harness/gateway/service.py` (импорты ~строки 46-88; `_run_bg` ~строка 615; `_resume_bg` ~строка 640; новый метод `_autotitle_bg` рядом с `_publish_finished` ~строка 744)
- Test: `tests/test_gateway_autotitle.py`

**Interfaces:**
- Consumes из Task 1: `needs_autotitle(title, meta)`, `fallback_title(task)`, `title_for(provider_factory, task, answer)`.
- Consumes существующее: `auxiliary_provider(models_cfg, store)` (`llm/openai_compatible.py:67`), `default_secret_store(path, env_fallback=True)` (`secrets/__init__.py:44`), `RunOutcome.final_answer` (`runtime/loop.py:149`), `self._read`, `self._spawn`, модели `Session`/`Run` (уже импортированы в service.py).
- Produces: фоновая задача `_autotitle_bg(run_id, answer)` — снаружи никто не вызывает; наблюдаемый эффект — обновлённые `sessions.title` и `meta["autotitle"]`.

- [ ] **Step 1: Написать падающие сервисные тесты**

Создать `tests/test_gateway_autotitle.py` (фикстура и скриптованный провайдер — копия паттерна `tests/test_gateway.py`, соседние `test_gateway_*.py` так же копируют):

```python
"""Автоназвание чатов по содержанию (спека 2026-08-04): хук GatewayService."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import ModelsConfig
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway import service as service_module
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolDefinition,
    Usage,
)
from svarog_harness.runtime import run_assembly
from svarog_harness.secrets import SecretStore
from svarog_harness.storage.models import Session


class ScriptedProvider(ModelProvider):
    """Основной агент: отдаёт заранее заданные ходы."""

    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        result = self.turns.pop(0)
        if on_text_delta is not None and result.content:
            on_text_delta(result.content)
        return result


class TitleProvider(ModelProvider):
    """Aux-модель названий: один и тот же ответ, считает вызовы."""

    def __init__(self, content: str = "Название чата", *, error: bool = False) -> None:
        self.content = content
        self.error = error
        self.calls = 0

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.calls += 1
        if self.error:
            raise RuntimeError("aux недоступна")
        return CompletionResult(content=self.content, usage=Usage(1, 1))


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
    cfg = load_config(project_dir=ws)
    return GatewayService(cfg, ws)


def _patch_agent(monkeypatch: pytest.MonkeyPatch, turns: list[CompletionResult]) -> None:
    provider = ScriptedProvider(turns)

    def fake_default_provider(
        models_cfg: ModelsConfig, store: object = None, workspace: object = None
    ) -> ModelProvider:
        return provider

    monkeypatch.setattr(run_assembly, "default_provider", fake_default_provider)


def _patch_title(monkeypatch: pytest.MonkeyPatch, provider: ModelProvider) -> None:
    def fake_auxiliary(models_cfg: ModelsConfig, store: SecretStore | None = None) -> ModelProvider:
        return provider

    monkeypatch.setattr(service_module, "auxiliary_provider", fake_auxiliary)


def _final(content: str) -> CompletionResult:
    return CompletionResult(content=content, usage=Usage(10, 5), finish_reason="stop")


async def _session_state(
    service: GatewayService, session_id: str
) -> tuple[str | None, dict[str, Any]]:
    async def action(db: Any) -> tuple[str | None, dict[str, Any]]:
        row = await db.get(Session, session_id)
        return row.title, dict(row.meta or {})

    return await service._read(action)


async def test_autotitle_after_first_run(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("Париж — столица Франции")])
    aux = TitleProvider("«География Франции.»")
    _patch_title(monkeypatch, aux)

    view = await service.create_session(title="Новый чат")
    await service.send_message(view.session_id, "Какая столица Франции?", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "География Франции"
    assert meta["autotitle"] == "done"
    assert aux.calls == 1


async def test_autotitle_fallback_when_aux_fails(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("ответ")])
    aux = TitleProvider(error=True)
    _patch_title(monkeypatch, aux)

    view = await service.create_session(title="Новый чат")
    await service.send_message(view.session_id, "Какая столица Франции?", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "Какая столица Франции?"  # короткий вопрос -> без обрезки
    assert meta["autotitle"] == "fallback"


async def test_autotitle_generated_once(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("первый ответ"), _final("второй ответ")])
    aux = TitleProvider("Название чата")
    _patch_title(monkeypatch, aux)

    view = await service.create_session(title="Новый чат")
    await service.send_message(view.session_id, "первый вопрос", None)
    await service.wait_for_background()
    await service.send_message(view.session_id, "второй вопрос", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "Название чата"
    assert meta["autotitle"] == "done"
    assert aux.calls == 1


async def test_autotitle_keeps_custom_title(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("ответ")])
    aux = TitleProvider("Не должно применяться")
    _patch_title(monkeypatch, aux)

    view = await service.create_session(title="Мой проект")
    await service.send_message(view.session_id, "вопрос", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "Мой проект"
    assert "autotitle" not in meta
    assert aux.calls == 0
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_gateway_autotitle.py -v`
Expected: FAIL — `AttributeError: module 'svarog_harness.gateway.service' has no attribute 'auxiliary_provider'` (или assert по title: остаётся «Новый чат»)

- [ ] **Step 3: Реализовать хук в service.py**

3a. Импорты (в существующие блоки import, по алфавиту):

```python
from svarog_harness.gateway.autotitle import fallback_title, needs_autotitle, title_for
from svarog_harness.llm.openai_compatible import auxiliary_provider
from svarog_harness.secrets import default_secret_store
```

(`default_secret_store` — добавить в существующий импорт из `svarog_harness.secrets`, если он уже есть; `Session`, `Run`, `select` уже импортированы.)

3b. В `_run_bg` (~строка 615) после `self._publish_finished(outcome)`:

```python
            self._publish_finished(outcome)
            self._spawn(self._autotitle_bg(outcome.run_id, outcome.final_answer))
```

3c. В `_resume_bg` (~строка 640) так же после `self._publish_finished(outcome)` — первый run мог уйти в approval и завершиться через resume:

```python
            self._publish_finished(outcome)
            self._spawn(self._autotitle_bg(outcome.run_id, outcome.final_answer))
```

3d. Новый метод рядом с `_publish_finished`:

```python
    async def _autotitle_bg(self, run_id: str, answer: str) -> None:
        """Автоназвание чата по первому обмену (спека 2026-08-04): best-effort.

        Отдельная фоновая задача после run_finished: сбой модели или БД не
        влияет на run. Флаг meta["autotitle"] делает попытку одноразовой;
        CLI-runs (title = task) отсекаются проверкой дефолтного названия.
        """
        try:
            async def read(db: AsyncSession) -> tuple[str, str] | None:
                run = await db.get(Run, run_id)
                if run is None:
                    return None
                session = await db.get(Session, run.session_id)
                if session is None or not needs_autotitle(session.title, session.meta):
                    return None
                first = (
                    await db.execute(
                        select(Run.task)
                        .where(Run.session_id == session.id)
                        .order_by(Run.created_at)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                return session.id, first or ""

            found = await self._read(read)
            if found is None:
                return
            session_id, first_task = found
            if not first_task.strip():
                return
            title = await title_for(
                lambda: auxiliary_provider(
                    self.cfg.models, default_secret_store(self.cfg.secrets.path)
                ),
                first_task,
                answer,
            )
            flag = "done" if title else "fallback"
            picked = title or fallback_title(first_task)
            if picked is None:
                return
            # mypy не сужает Optional для переменных, захваченных замыканием, —
            # поэтому в write уходит уже str-копия.
            final_title: str = picked

            async def write(db: AsyncSession) -> None:
                session = await db.get(Session, session_id)
                if session is None or not needs_autotitle(session.title, session.meta):
                    return  # гонка: параллельный run уже назвал
                session.title = final_title
                # JSON-колонка без MutableDict: изменение фиксируется только
                # присваиванием нового dict, не мутацией на месте.
                session.meta = {**(session.meta or {}), "autotitle": flag}
                await db.commit()

            await self._read(write)
        except Exception:
            # Автоназвание никогда не роняет фоновую задачу (best-effort, спека).
            return
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest tests/test_gateway_autotitle.py -v`
Expected: PASS (4 теста)

- [ ] **Step 5: Прогнать соседние тесты gateway (регрессия хука в _run_bg/_resume_bg)**

Run: `uv run pytest tests/test_gateway.py tests/test_gateway_completion.py tests/test_autotitle.py -q`
Expected: PASS, без новых падений

- [ ] **Step 6: Линт, типы, коммит**

```bash
uv run ruff check src/svarog_harness/gateway tests/test_gateway_autotitle.py && uv run ruff format src/svarog_harness/gateway tests/test_gateway_autotitle.py && uv run mypy src/svarog_harness/gateway
git add src/svarog_harness/gateway/service.py tests/test_gateway_autotitle.py
git commit -m "feat(gateway): автоназвание чатов по содержанию после первого run'а"
```

---

## Проверка вживую (после обеих задач, вручную)

1. Запустить gateway с web UI, создать чат, отправить сообщение, дождаться ответа.
2. В течение пары секунд поллинга название в сайдбаре меняется с «Новый чат» на сгенерированное.
3. `web/dist` пересобирать не нужно — фронтенд не менялся.
