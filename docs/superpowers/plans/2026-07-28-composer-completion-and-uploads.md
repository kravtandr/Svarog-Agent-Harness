# Поле ввода: команды, `@`-файлы, вложения — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** поле ввода веб-чата ведёт себя как CLI-чат — слэш-команды с подсказками, ссылки на файлы через `@`, вложения к сообщению — и называет исполнителя по адаптеру, а автономию так же, как «Настройки».

**Architecture:** почти вся логика уже написана для CLI. Работа состоит в том, чтобы вынести её в места, откуда её видит и шлюз (реестр адаптеров, обход workspace, реестр команд), отдать наружу эндпоинтами и повторить в клиенте только то, что обязано жить в браузере — определение режима подсказок. Вложение — файл в `.attachments/` внутри workspace сессии, исключённой из git через `.git/info/exclude`; агент читает его существующими `read_image`/`read_document`.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI (+ `python-multipart`), SQLAlchemy async; клиент — React 19 + Vite 6 + TypeScript 5, Vitest 3 + @testing-library/react.

Спек: `docs/superpowers/specs/2026-07-28-composer-completion-and-uploads-design.md`.

## Global Constraints

- Ветка: работа начинается с `main` (прошлая ветка влита). Первая задача заводит `feat/composer-completion`.
- Комментарии, тексты ошибок, надписи интерфейса и сообщения коммитов — по-русски, как в окружающем коде.
- Значения секретов не логируются и не возвращаются (ADR-0006).
- Конфиг не меняется под работающим запуском (ADR-0015 §0.4).
- Путь любого файла обязан оставаться внутри своего корня: проверка `is_relative_to` после `resolve()`, fail-closed.
- Гейты, все зелёные перед коммитом:
  - `COLUMNS=200 .venv/bin/python -m pytest -q`
  - `.venv/bin/ruff check src tests` и `.venv/bin/ruff format --check .`
  - `.venv/bin/mypy`
  - `npm --prefix web test` (включает `prettier --check src`) и `npm --prefix web run build`
- `tests/test_cancel_running_cooperative` — известная предсуществующая флака по таймингу. Упало — перезапустить изолированно, подтвердить и не гоняться.

## Структура файлов

**Создаются:**

| Файл | Ответственность |
|---|---|
| `src/svarog_harness/gateway/executors.py` | Варианты исполнителя для селекта: каталог + detection по `PATH` |
| `src/svarog_harness/gateway/commands.py` | Реестр слэш-команд веба |
| `src/svarog_harness/gateway/attachments.py` | Белый список, санитизация имени, запись в `.attachments/`, исключение из git |
| `tests/test_gateway_executors.py` | Адаптер в override, подмена образа, `GET /executors` |
| `tests/test_gateway_completion.py` | `GET /commands`, `GET /sessions/{id}/files` |
| `tests/test_gateway_attachments.py` | Загрузка, лимиты, санитизация, `.git/info/exclude`, `attachments` в сообщении |
| `web/src/model/completion.ts` | Порт `detect_completion` |
| `web/src/model/completion.test.ts` | Случаи из `tests/test_chat_completion.py` |
| `web/src/components/Completion.tsx` + `.css` + `.test.tsx` | Меню подсказок |
| `web/src/components/Attachments.tsx` + `.css` + `.test.tsx` | Чипы вложений |

**Меняются:**

| Файл | Что |
|---|---|
| `src/svarog_harness/runtime/agents/__init__.py` | `EXTERNAL_ADAPTERS`, `ADAPTER_BINARIES` — общий дом реестра |
| `src/svarog_harness/cli/chat_display.py` | Импорт реестра оттуда вместо своих приватных констант |
| `src/svarog_harness/gitflow/repo.py` | `GitRepo.git_dir()` |
| `src/svarog_harness/gateway/overrides.py` | Поле `adapter`, подмена адаптера и образа |
| `src/svarog_harness/gateway/models.py` | `ExecutorOptionView`, `SlashCommandView`, `FileSuggestionView`, `AttachmentView`, поля `adapter` и `attachments` |
| `src/svarog_harness/gateway/service.py` | `executor_options`, `command_registry`, `file_suggestions`, `store_attachment`, `attachments` в `send_message` |
| `src/svarog_harness/gateway/api.py` | `GET /executors`, `GET /commands`, `GET /sessions/{id}/files`, `POST /sessions/{id}/attachments` |
| `pyproject.toml` | `python-multipart` в extra `server` |
| `web/src/api/types.ts`, `client.ts`, `test/fakeApi.ts` | Типы и методы новых эндпоинтов |
| `web/src/components/Composer.tsx` + `.css` | Меню подсказок, чипы, скрепка, сырые подписи автономии, селект по адаптерам |
| `web/src/screens/ChatScreen.tsx` | Состояние подсказок и вложений, действия команд |
| `web/src/model/thread.ts`, `web/src/screens/ChatScreen.css` | Миниатюра вложения в ленте |

---

### Задача 1: реестр адаптеров переезжает в общий дом

Сейчас список адаптеров и их бинарей — приватные константы `cli/chat_display.py`. Шлюзу они нужны тоже, а импортировать `cli` из `gateway` нельзя: это разные слои.

**Files:**
- Modify: `src/svarog_harness/runtime/agents/__init__.py`
- Modify: `src/svarog_harness/cli/chat_display.py:24-29, 201-204`
- Test: `tests/test_gateway_executors.py`

**Interfaces:**
- Produces: `EXTERNAL_ADAPTERS: tuple[str, ...]`, `ADAPTER_BINARIES: dict[str, str]`, `adapter_available(name: str) -> bool` — все из `svarog_harness.runtime.agents`.

- [ ] **Шаг 1: завести ветку**

```bash
git checkout -b feat/composer-completion
```

- [ ] **Шаг 2: тест**

```python
"""Варианты исполнителя для селекта поля ввода (план 2026-07-28)."""

from svarog_harness.runtime.agents import (
    ADAPTER_BINARIES,
    EXTERNAL_ADAPTERS,
    adapter_available,
)


def test_registry_lists_every_adapter_with_its_binary() -> None:
    assert EXTERNAL_ADAPTERS == ("claude-code", "codex", "opencode")
    assert set(ADAPTER_BINARIES) == set(EXTERNAL_ADAPTERS)
    assert ADAPTER_BINARIES["claude-code"] == "claude"


def test_availability_is_a_path_lookup(monkeypatch) -> None:
    monkeypatch.setattr(
        "svarog_harness.runtime.agents.shutil.which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )
    assert adapter_available("claude-code") is True
    assert adapter_available("codex") is False
    assert adapter_available("нет-такого") is False
```

- [ ] **Шаг 3: убедиться, что падает**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_executors.py -q`
Expected: FAIL — `ImportError: cannot import name 'EXTERNAL_ADAPTERS'`

- [ ] **Шаг 4: перенести реестр**

В `runtime/agents/__init__.py` добавить `import shutil` и:

```python
# Реестр адаптеров внешнего агента (ADR-0016). Живёт здесь, а не в CLI:
# и chat, и gateway строят по нему список исполнителей, а импортировать
# cli из gateway нельзя — это разные слои.
EXTERNAL_ADAPTERS: tuple[str, ...] = ("claude-code", "codex", "opencode")
ADAPTER_BINARIES: dict[str, str] = {
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
}


def adapter_available(name: str) -> bool:
    """Есть ли host-CLI адаптера в PATH (detection для каталога исполнителей)."""
    binary = ADAPTER_BINARIES.get(name)
    return binary is not None and shutil.which(binary) is not None
```

Добавить все три имени в `__all__`.

В `cli/chat_display.py` удалить приватные `_EXTERNAL_ADAPTERS`, `_ADAPTER_BINARIES` и `_adapter_available`, импортировать общие и заменить вызов в `chat_status_view` на `adapter_available(adapter)`.

- [ ] **Шаг 5: тесты зелёные**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_executors.py tests/test_chat_display.py -q`
Expected: PASS

- [ ] **Шаг 6: полный прогон и коммит**

```bash
COLUMNS=200 .venv/bin/python -m pytest -q && .venv/bin/ruff check src tests && .venv/bin/ruff format --check . && .venv/bin/mypy
git add src tests && git commit -m "refactor(runtime): реестр адаптеров переезжает из cli в agents"
```

---

### Задача 2: адаптер в override и подмена образа

**Files:**
- Modify: `src/svarog_harness/gateway/overrides.py`
- Test: `tests/test_gateway_executors.py`

**Interfaces:**
- Consumes: `EXTERNAL_ADAPTERS` (задача 1).
- Produces: `RunOverride.adapter: str | None`; `apply_override` учитывает адаптер.

- [ ] **Шаг 1: тесты**

Дописать в `tests/test_gateway_executors.py`:

```python
from pathlib import Path

import pytest

from svarog_harness.config.loader import load_config
from svarog_harness.gateway.overrides import OverrideError, RunOverride, apply_override
from svarog_harness.scaffold import DEFAULT_CLAUDE_IMAGE, DEFAULT_OPENCODE_IMAGE


def _config(tmp_path: Path, image: str = DEFAULT_CLAUDE_IMAGE) -> object:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: docker\n"
        "executor:\n"
        "  type: external\n"
        "  external:\n"
        "    adapter: claude-code\n"
        f"    image: {image}\n",
        encoding="utf-8",
    )
    return load_config(project_dir=ws, user_config_path=tmp_path / "нет")


def test_adapter_switches_adapter_and_default_image(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    derived = apply_override(cfg, RunOverride(executor="external", adapter="opencode"))
    assert derived.executor.external.adapter == "opencode"
    assert derived.executor.external.image == DEFAULT_OPENCODE_IMAGE


def test_custom_image_is_left_alone(tmp_path: Path) -> None:
    cfg = _config(tmp_path, image="registry.example/мой-агент:7")
    derived = apply_override(cfg, RunOverride(executor="external", adapter="opencode"))
    assert derived.executor.external.adapter == "opencode"
    assert derived.executor.external.image == "registry.example/мой-агент:7"


def test_codex_without_own_image_is_rejected(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(OverrideError, match="codex"):
        apply_override(cfg, RunOverride(executor="external", adapter="codex"))


def test_codex_allowed_when_image_is_custom(tmp_path: Path) -> None:
    cfg = _config(tmp_path, image="registry.example/codex:1")
    derived = apply_override(cfg, RunOverride(executor="external", adapter="codex"))
    assert derived.executor.external.adapter == "codex"


def test_adapter_without_external_executor_is_rejected(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(OverrideError, match="native"):
        apply_override(cfg, RunOverride(executor="native", adapter="opencode"))


def test_adapter_round_trips_through_meta() -> None:
    ov = RunOverride(executor="external", adapter="opencode")
    assert ov.to_meta() == {"executor": "external", "adapter": "opencode"}
    assert RunOverride.from_meta({"override": ov.to_meta()}) == ov
    assert RunOverride.from_meta({"override": {"adapter": 42}}).adapter is None
```

- [ ] **Шаг 2: убедиться, что падают**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_executors.py -q -k adapter`
Expected: FAIL — `RunOverride` не принимает `adapter`

- [ ] **Шаг 3: реализация**

В `overrides.py` добавить поле в датакласс, включить его в `to_meta`/`from_meta` (валидация — `in EXTERNAL_ADAPTERS`), и в `apply_override` перед веткой executor:

```python
# Образы per-adapter: те же дефолты, что пишет `svarog init`. Подменяем
# образ вместе с адаптером, иначе в sandbox остаётся CLI прежнего агента и
# запуск падает `command not found`. Кастомный образ не трогаем — его
# поставили руками, и подмена молча увела бы запуск в другой контейнер.
_ADAPTER_IMAGES: dict[str, str] = {
    "claude-code": DEFAULT_CLAUDE_IMAGE,
    "opencode": DEFAULT_OPENCODE_IMAGE,
}
```

и внутри функции:

```python
    if ov.adapter is not None:
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
        external = cfg.executor.external.model_copy(update=update_external)
        update["executor"] = cfg.executor.model_copy(
            update={"type": "external", "external": external}
        )
```

Ветка `ov.executor` ниже не должна затирать этот результат: если `adapter` задан, `executor` уже учтён здесь.

- [ ] **Шаг 4: тесты зелёные**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_executors.py tests/test_gateway_overrides.py -q`
Expected: PASS

- [ ] **Шаг 5: коммит**

```bash
git add src/svarog_harness/gateway/overrides.py tests/test_gateway_executors.py
git commit -m "feat(gateway): адаптер внешнего агента в override сообщения"
```

---

### Задача 3: `GET /executors`

**Files:**
- Create: `src/svarog_harness/gateway/executors.py`
- Modify: `src/svarog_harness/gateway/models.py`, `service.py`, `api.py`
- Test: `tests/test_gateway_executors.py`

**Interfaces:**
- Consumes: `EXTERNAL_ADAPTERS`, `adapter_available` (задача 1).
- Produces: `executor_options(cfg) -> list[ExecutorOption]`; `ExecutorOptionView`; `GatewayService.executor_options()`; `GET /executors`.

- [ ] **Шаг 1: тесты**

```python
from fastapi.testclient import TestClient

from svarog_harness.gateway.api import create_app
from svarog_harness.gateway.executors import executor_options


def test_native_always_present_and_active_matches_config(tmp_path: Path) -> None:
    cfg = _config(tmp_path)  # executor.type = external, adapter = claude-code
    options = executor_options(cfg)
    by_value = {o.value: o for o in options}
    assert by_value["native"].kind == "native"
    assert by_value["native"].available is True
    assert by_value["claude-code"].is_active is True
    assert by_value["native"].is_active is False


def test_configured_adapter_is_available_even_without_cli(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "svarog_harness.gateway.executors.adapter_available", lambda _: False
    )
    options = {o.value: o for o in executor_options(_config(tmp_path))}
    assert options["claude-code"].available is True, "прописан в конфиге"
    assert options["opencode"].available is False
    assert "codex" in options, "недоступный адаптер показывается, а не прячется"


def test_executors_endpoint(service) -> None:
    client = TestClient(create_app(service=service))
    body = client.get("/executors").json()
    assert [o["value"] for o in body][0] == "native"
    assert all("available" in o and "is_active" in o for o in body)
```

Фикстуру `service` взять по образцу `tests/test_gateway_web.py` (конфиг в `tmp_path`, `monkeypatch.setenv("HOME", …)`).

- [ ] **Шаг 2: убедиться, что падают**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_executors.py -q -k executor_options`
Expected: FAIL — модуль `gateway.executors` не найден

- [ ] **Шаг 3: модуль**

```python
"""Варианты исполнителя для селекта поля ввода.

Тот же принцип, что у `cli/chat_display.chat_status_view`: native всегда,
плюс каждый адаптер, который прописан в конфиге или чей CLI нашёлся в PATH.
Недоступные не прячем — иначе человек не понимает, почему в списке нет
codex, и думает, что Сварог его не умеет.
"""

from dataclasses import dataclass
from typing import Literal

from svarog_harness.config.schema import SvarogConfig
from svarog_harness.runtime.agents import EXTERNAL_ADAPTERS, adapter_available


@dataclass(frozen=True)
class ExecutorOption:
    value: str
    kind: Literal["native", "external"]
    adapter: str | None
    available: bool
    is_active: bool


def executor_options(cfg: SvarogConfig) -> list[ExecutorOption]:
    configured = cfg.executor.external.adapter if cfg.executor.external is not None else None
    native_active = cfg.executor.type == "native"
    options = [
        ExecutorOption(
            value="native", kind="native", adapter=None, available=True, is_active=native_active
        )
    ]
    for adapter in EXTERNAL_ADAPTERS:
        options.append(
            ExecutorOption(
                value=adapter,
                kind="external",
                adapter=adapter,
                available=adapter == configured or adapter_available(adapter),
                is_active=not native_active and adapter == configured,
            )
        )
    return options
```

- [ ] **Шаг 4: модель ответа, сервис, эндпоинт**

`models.py`:

```python
class ExecutorOptionView(BaseModel):
    value: str
    kind: Literal["native", "external"]
    adapter: str | None = None
    available: bool
    is_active: bool
```

`service.py` — метод рядом с `list_providers`:

```python
    def executor_options(self) -> list[ExecutorOption]:
        """Варианты исполнителя по текущему конфигу и наличию CLI адаптеров."""
        return executor_options(self.cfg)
```

`api.py`:

```python
    @app.get("/executors", response_model=list[ExecutorOptionView])
    async def list_executors(service: ServiceDep) -> list[ExecutorOptionView]:
        return [ExecutorOptionView(**vars(option)) for option in service.executor_options()]
```

- [ ] **Шаг 5: тесты зелёные и коммит**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_executors.py -q`

```bash
git add src tests && git commit -m "feat(gateway): список исполнителей с detection адаптеров"
```

---

### Задача 4: реестр команд и `GET /commands`

**Files:**
- Create: `src/svarog_harness/gateway/commands.py`
- Modify: `src/svarog_harness/gateway/models.py`, `service.py`, `api.py`
- Test: `tests/test_gateway_completion.py`

**Interfaces:**
- Produces: `WEB_COMMANDS: tuple[SlashCommand, ...]`; `SlashCommandView`; `GET /commands`.

- [ ] **Шаг 1: тест**

```python
"""Слэш-команды и подсказки файлов для поля ввода (план 2026-07-28)."""

from fastapi.testclient import TestClient

from svarog_harness.gateway.api import create_app
from svarog_harness.gateway.commands import WEB_COMMANDS


def test_registry_has_six_web_commands() -> None:
    names = [cmd.name for cmd in WEB_COMMANDS]
    assert names == ["help", "new", "sessions", "executor", "policies", "copy"]
    assert all(cmd.help for cmd in WEB_COMMANDS), "у каждой команды есть описание"


def test_commands_endpoint(service) -> None:
    client = TestClient(create_app(service=service))
    body = client.get("/commands").json()
    assert [c["name"] for c in body] == [cmd.name for cmd in WEB_COMMANDS]
    assert body[0]["usage"].startswith("/")
```

- [ ] **Шаг 2: убедиться, что падает**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_completion.py -q`
Expected: FAIL — модуль `gateway.commands` не найден

- [ ] **Шаг 3: реализация**

```python
"""Слэш-команды веб-чата.

Реестр свой, а не CLI-шный: `/quit` и `/mode` в браузере бессмысленны, а
`/fork` требует серверной поддержки форка сессии, которой пока нет.
Дедупликация с CLI не нужна — пересекается только тип SlashCommand.
"""

from svarog_harness.cli.chat_commands import SlashCommand

WEB_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("help", "/help", "показать команды"),
    SlashCommand("new", "/new", "новый чат"),
    SlashCommand("sessions", "/sessions", "перейти к списку чатов"),
    SlashCommand("executor", "/executor", "выбрать исполнителя"),
    SlashCommand("policies", "/policies", "выбрать автономию"),
    SlashCommand("copy", "/copy", "скопировать последний ответ"),
)
```

Импорт `SlashCommand` из `cli` в `gateway` — единственное исключение и оно
осознанное: это чистый датакласс без поведения. Если появится второе
такое место, тип переезжает в общий модуль.

`models.py`:

```python
class SlashCommandView(BaseModel):
    name: str
    usage: str
    help: str
```

`api.py`:

```python
    @app.get("/commands", response_model=list[SlashCommandView])
    async def list_commands() -> list[SlashCommandView]:
        return [SlashCommandView(**vars(cmd)) for cmd in WEB_COMMANDS]
```

- [ ] **Шаг 4: тесты зелёные и коммит**

```bash
COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_completion.py -q
git add src tests && git commit -m "feat(gateway): реестр слэш-команд веб-чата"
```

---

### Задача 5: `GET /sessions/{id}/files`

**Files:**
- Modify: `src/svarog_harness/gateway/models.py`, `service.py`, `api.py`
- Test: `tests/test_gateway_completion.py`

**Interfaces:**
- Produces: `FileSuggestionView`; `GatewayService.file_suggestions(session_id, query) -> list[Suggestion]`; `GET /sessions/{id}/files?q=`.

- [ ] **Шаг 1: тесты**

```python
import pytest


@pytest.mark.asyncio
async def test_file_suggestions_come_from_session_workspace(service) -> None:
    session = await service.create_session(title="файлы")
    ws = service.workspace
    (ws / "заметка.md").write_text("текст", encoding="utf-8")
    (ws / "node_modules").mkdir(exist_ok=True)
    (ws / "node_modules" / "мусор.js").write_text("//", encoding="utf-8")
    (ws / ".attachments").mkdir(exist_ok=True)
    (ws / ".attachments" / "скрин.png").write_bytes(b"\x89PNG")

    found = await service.file_suggestions(session.session_id, "@")
    paths = [s.value for s in found]

    assert "@заметка.md" in paths
    assert not any("node_modules" in p for p in paths), "тяжёлые каталоги отфильтрованы"
    assert not any(".attachments" in p for p in paths), "вложения не засоряют подсказки"


@pytest.mark.asyncio
async def test_files_endpoint_filters_by_query(service) -> None:
    session = await service.create_session(title="фильтр")
    (service.workspace / "readme.md").write_text("x", encoding="utf-8")
    (service.workspace / "прочее.txt").write_text("x", encoding="utf-8")
    client = TestClient(create_app(service=service))

    body = client.get(f"/sessions/{session.session_id}/files", params={"q": "@read"}).json()

    assert [item["path"] for item in body] == ["readme.md"]


@pytest.mark.asyncio
async def test_files_endpoint_unknown_session_is_404(service) -> None:
    client = TestClient(create_app(service=service))
    assert client.get("/sessions/нет/files").status_code == 404
```

- [ ] **Шаг 2: убедиться, что падают**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_completion.py -q -k file`
Expected: FAIL — у сервиса нет `file_suggestions`

- [ ] **Шаг 3: сервис**

```python
    async def file_suggestions(self, session_id: str, query: str) -> list[Suggestion]:
        """Подсказки `@file` по workspace сессии.

        Корень — workspace именно сессии, а не сервиса: у сессии может быть
        своя рабочая папка (ADR-0017), и подсказки обязаны показывать те
        файлы, которые агент этой сессии действительно увидит.
        """

        async def action(db: AsyncSession) -> dict[str, object]:
            session = await find_session_by_prefix(db, session_id)
            return dict(session.meta or {})

        meta = await self._read(action)
        workspace = Path(str(meta.get("workspace") or self.workspace))
        token = query if query.startswith("@") else f"@{query}"
        return at_suggestions(workspace, token)
```

`list_workspace_files` уже пропускает каталоги, начинающиеся с точки, поэтому `.attachments/` в подсказки не попадёт без дополнительного кода — тест это фиксирует.

- [ ] **Шаг 4: модель и эндпоинт**

```python
class FileSuggestionView(BaseModel):
    path: str
    label: str
```

```python
    @app.get("/sessions/{session_id}/files", response_model=list[FileSuggestionView])
    async def session_files(
        session_id: str, service: ServiceDep, q: str = ""
    ) -> list[FileSuggestionView]:
        try:
            found = await service.file_suggestions(session_id, q)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return [
            FileSuggestionView(path=s.value.removeprefix("@"), label=s.label) for s in found
        ]
```

- [ ] **Шаг 5: тесты зелёные и коммит**

```bash
COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_completion.py -q
git add src tests && git commit -m "feat(gateway): подсказки файлов по workspace сессии"
```

---

### Задача 6: `GitRepo.git_dir()` и исключение из git

**Files:**
- Modify: `src/svarog_harness/gitflow/repo.py`
- Create: `src/svarog_harness/gateway/attachments.py` (частично — только исключение)
- Test: `tests/test_gateway_attachments.py`

**Interfaces:**
- Produces: `GitRepo.git_dir() -> Path | None`; `async ensure_git_excluded(workspace: Path, pattern: str) -> None`.

- [ ] **Шаг 1: тесты**

```python
"""Вложения к сообщению: запись, лимиты, исключение из git (план 2026-07-28)."""

from pathlib import Path

import pytest

from svarog_harness.gateway.attachments import ensure_git_excluded
from svarog_harness.gitflow import GitRepo


@pytest.mark.asyncio
async def test_git_dir_resolves_separate_git_dir(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    gitdir = tmp_path / "gitdir"
    repo = GitRepo(ws)
    await repo.init(separate_git_dir=gitdir)

    found = await repo.git_dir()

    assert found is not None
    assert found.resolve() == gitdir.resolve(), "не workspace/.git, а настоящий каталог"


@pytest.mark.asyncio
async def test_exclude_is_written_once(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    repo = GitRepo(ws)
    await repo.init()

    await ensure_git_excluded(ws, ".attachments/")
    await ensure_git_excluded(ws, ".attachments/")

    exclude = (await repo.git_dir()) / "info" / "exclude"
    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert lines.count(".attachments/") == 1, "повторный вызов не дублирует строку"


@pytest.mark.asyncio
async def test_exclude_is_noop_without_git(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    await ensure_git_excluded(ws, ".attachments/")  # не падает
```

- [ ] **Шаг 2: убедиться, что падают**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_attachments.py -q`
Expected: FAIL — у `GitRepo` нет `git_dir`

- [ ] **Шаг 3: `GitRepo.git_dir`**

Рядом с `toplevel` в `repo.py`:

```python
    async def git_dir(self) -> Path | None:
        """Настоящий каталог git; при separate_git_dir — не `workspace/.git`."""
        code, out, _ = await self._git("rev-parse", "--absolute-git-dir", check=False)
        if code != 0 or not out.strip():
            return None
        return Path(out.strip())
```

- [ ] **Шаг 4: `ensure_git_excluded`**

```python
"""Вложения к сообщению: приём файла и его сокрытие от git."""

from pathlib import Path

from svarog_harness.gitflow import GitRepo

# Каталог вложений внутри workspace. Точка в начале — чтобы
# `list_workspace_files` его пропускал и вложения не засоряли подсказки `@`.
ATTACHMENTS_DIR = ".attachments"


async def ensure_git_excluded(workspace: Path, pattern: str) -> None:
    """Дописать pattern в `.git/info/exclude` рабочего дерева, идемпотентно.

    Именно `info/exclude`, а не `.gitignore`: этот файл локальный и не
    отслеживается, поэтому служебное исключение не попадёт в чужой diff.
    Без него автокоммит (Flow C) утащил бы вложения в историю task-ветки.
    """
    git_dir = await GitRepo(workspace).git_dir()
    if git_dir is None:
        return  # рабочая папка без git — прятать не от кого
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if pattern in existing.splitlines():
        return
    prefix = "" if existing.endswith("\n") or not existing else "\n"
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(f"{prefix}{pattern}\n")
```

- [ ] **Шаг 5: тесты зелёные и коммит**

```bash
COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_attachments.py -q
git add src tests && git commit -m "feat(gitflow): git_dir и локальное исключение вложений"
```

---

### Задача 7: приём вложения

**Files:**
- Modify: `src/svarog_harness/gateway/attachments.py`, `models.py`, `service.py`, `api.py`, `pyproject.toml`
- Test: `tests/test_gateway_attachments.py`

**Interfaces:**
- Consumes: `ATTACHMENTS_DIR`, `ensure_git_excluded` (задача 6).
- Produces: `ALLOWED_SUFFIXES`, `MAX_UPLOAD_BYTES`, `AttachmentTooLarge`, `AttachmentTypeError`, `safe_name(name) -> str`, `async store_attachment(workspace, name, data) -> StoredAttachment`; `AttachmentView`; `POST /sessions/{id}/attachments`.

- [ ] **Шаг 1: тесты**

```python
from svarog_harness.gateway.attachments import (
    ALLOWED_SUFFIXES,
    MAX_UPLOAD_BYTES,
    AttachmentTooLarge,
    AttachmentTypeError,
    safe_name,
    store_attachment,
)


def test_allowed_suffixes_are_built_from_tools_not_copied() -> None:
    from svarog_harness.tools.document_tools import _DOCUMENT_SUFFIXES, _IMAGE_MIME

    assert set(_IMAGE_MIME) <= ALLOWED_SUFFIXES
    assert set(_DOCUMENT_SUFFIXES) <= ALLOWED_SUFFIXES
    assert ".txt" in ALLOWED_SUFFIXES and ".md" in ALLOWED_SUFFIXES


@pytest.mark.parametrize(
    "raw",
    ["../../etc/passwd", "foo/../../bar.png", "/абсолютный.png", "..\\win.png"],
)
def test_safe_name_strips_directories(raw: str) -> None:
    assert "/" not in safe_name(raw) and "\\" not in safe_name(raw)
    assert not safe_name(raw).startswith("..")


@pytest.mark.asyncio
async def test_store_puts_file_in_attachments_and_keeps_original_name(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    stored = await store_attachment(ws, "скрин бага.png", b"\x89PNG данные")

    assert stored.name == "скрин бага.png"
    assert stored.path.startswith(".attachments/")
    assert (ws / stored.path).read_bytes() == b"\x89PNG данные"
    assert (ws / stored.path).resolve().is_relative_to((ws / ".attachments").resolve())


@pytest.mark.asyncio
async def test_same_name_twice_gives_two_files(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    first = await store_attachment(ws, "скрин.png", b"1")
    second = await store_attachment(ws, "скрин.png", b"2")

    assert first.path != second.path, "коллизий нет по построению, отказывать не нужно"


@pytest.mark.asyncio
async def test_unknown_suffix_rejected(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(AttachmentTypeError, match=".png"):
        await store_attachment(ws, "вирус.exe", b"MZ")


@pytest.mark.asyncio
async def test_too_large_rejected(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(AttachmentTooLarge):
        await store_attachment(ws, "большой.png", b"x" * (MAX_UPLOAD_BYTES + 1))


@pytest.mark.asyncio
async def test_image_over_vision_limit_is_flagged_not_refused(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    stored = await store_attachment(ws, "огромный.png", b"x" * (6 * 1024 * 1024))
    assert stored.too_large_for_vision is True
```

- [ ] **Шаг 2: убедиться, что падают**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_attachments.py -q -k store or safe_name`
Expected: FAIL — нет `store_attachment`

- [ ] **Шаг 3: реализация**

Дописать в `attachments.py`:

```python
import hashlib
from dataclasses import dataclass

from svarog_harness.tools.document_tools import (
    _DOCUMENT_SUFFIXES,
    _IMAGE_LIMIT_BYTES,
    _IMAGE_MIME,
)

# Белый список строится из того, что инструменты действительно умеют, а не
# копируется: иначе разойдётся с ними при первом расширении набора.
ALLOWED_SUFFIXES: frozenset[str] = frozenset(
    set(_IMAGE_MIME) | set(_DOCUMENT_SUFFIXES) | {".txt", ".md", ".json", ".yaml", ".yml"}
)
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class AttachmentTypeError(ValueError):
    """Расширение вне белого списка; наружу — HTTP 415."""


class AttachmentTooLarge(ValueError):
    """Файл больше потолка; наружу — HTTP 413."""


@dataclass(frozen=True)
class StoredAttachment:
    path: str  # относительно workspace: ".attachments/ab12_скрин.png"
    name: str  # исходное имя, как его видит человек
    size_bytes: int
    mime: str | None
    too_large_for_vision: bool


def safe_name(raw: str) -> str:
    """Только базовое имя: разделители путей и `..` отбрасываются."""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = name.lstrip(".") if name.startswith("..") else name
    return name or "файл"


async def store_attachment(workspace: Path, name: str, data: bytes) -> StoredAttachment:
    """Записать вложение в `.attachments/` и спрятать каталог от git."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise AttachmentTooLarge(
            f"файл {len(data)} байт больше потолка {MAX_UPLOAD_BYTES}"
        )
    clean = safe_name(name)
    suffix = Path(clean).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        allowed = ", ".join(sorted(ALLOWED_SUFFIXES))
        raise AttachmentTypeError(f"расширение '{suffix}' не поддержано; доступны: {allowed}")

    root = (workspace / ATTACHMENTS_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Префикс-хеш от содержимого и имени: коллизий нет, отказывать при
    # повторной загрузке не нужно, а человеку показывается исходное имя.
    digest = hashlib.sha256(data + clean.encode("utf-8")).hexdigest()[:8]
    target = (root / f"{digest}_{clean}").resolve()
    if not target.is_relative_to(root):
        raise AttachmentTypeError(f"имя '{name}' выводит за пределы {ATTACHMENTS_DIR}")
    target.write_bytes(data)
    await ensure_git_excluded(workspace, f"{ATTACHMENTS_DIR}/")

    mime = _IMAGE_MIME.get(suffix)
    return StoredAttachment(
        path=f"{ATTACHMENTS_DIR}/{target.name}",
        name=clean,
        size_bytes=len(data),
        mime=mime,
        too_large_for_vision=mime is not None and len(data) > _IMAGE_LIMIT_BYTES,
    )
```

- [ ] **Шаг 4: эндпоинт**

`pyproject.toml`, extra `server`: добавить `"python-multipart>=0.0.9"` — до сих пор он приходил транзитивно через `mcp`, а теперь нужен нам напрямую.

`models.py`:

```python
class AttachmentView(BaseModel):
    path: str
    name: str
    size_bytes: int
    mime: str | None = None
    too_large_for_vision: bool = False
```

`service.py`:

```python
    async def store_attachment(self, session_id: str, name: str, data: bytes) -> StoredAttachment:
        """Положить вложение в workspace сессии; под живой запуск — отказ."""

        async def action(db: AsyncSession) -> tuple[str, dict[str, object]]:
            session = await find_session_by_prefix(db, session_id)
            live = (
                await db.execute(
                    select(Run)
                    .where(Run.session_id == session.id, Run.state.in_(_LIVE_STATES))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if live is not None:
                raise SessionBusyError(
                    "в этом чате идёт запуск — дождитесь конца, прежде чем прикреплять файлы"
                )
            return session.id, dict(session.meta or {})

        _, meta = await self._read(action)
        workspace = Path(str(meta.get("workspace") or self.workspace))
        return await store_attachment(workspace, name, data)
```

`api.py`:

```python
    @app.post(
        "/sessions/{session_id}/attachments",
        response_model=AttachmentView,
        status_code=201,
    )
    async def upload_attachment(
        session_id: str, service: ServiceDep, file: UploadFile = File(...)
    ) -> AttachmentView:
        data = await file.read()
        try:
            stored = await service.store_attachment(session_id, file.filename or "файл", data)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except AttachmentTypeError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from None
        except AttachmentTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from None
        return AttachmentView(**vars(stored))
```

- [ ] **Шаг 5: тесты эндпоинта**

```python
@pytest.mark.asyncio
async def test_upload_endpoint(service) -> None:
    session = await service.create_session(title="вложение")
    client = TestClient(create_app(service=service))

    response = client.post(
        f"/sessions/{session.session_id}/attachments",
        files={"file": ("скрин.png", b"\x89PNG", "image/png")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "скрин.png"
    assert body["path"].startswith(".attachments/")
    assert (service.workspace / body["path"]).is_file()


@pytest.mark.asyncio
async def test_upload_rejects_bad_suffix_with_415(service) -> None:
    session = await service.create_session(title="плохое")
    client = TestClient(create_app(service=service))
    response = client.post(
        f"/sessions/{session.session_id}/attachments",
        files={"file": ("вирус.exe", b"MZ", "application/octet-stream")},
    )
    assert response.status_code == 415
```

- [ ] **Шаг 6: полный прогон и коммит**

```bash
COLUMNS=200 .venv/bin/python -m pytest -q && .venv/bin/ruff check src tests && .venv/bin/mypy
git add src tests pyproject.toml && git commit -m "feat(gateway): приём вложений в .attachments"
```

---

### Задача 8: вложения доезжают до агента

**Files:**
- Modify: `src/svarog_harness/gateway/models.py`, `service.py`, `api.py`
- Test: `tests/test_gateway_attachments.py`

**Interfaces:**
- Consumes: `ATTACHMENTS_DIR` (задача 6).
- Produces: `SendMessageRequest.attachments: list[str]`; `send_message(..., attachments=())`.

- [ ] **Шаг 1: тесты**

```python
@pytest.mark.asyncio
async def test_attachment_paths_are_appended_to_task_text(service) -> None:
    session = await service.create_session(title="с вложением")
    stored = await service.store_attachment(session.session_id, "скрин.png", b"\x89PNG")

    run_id = await service.send_message(
        session.session_id, "посмотри баг", None, attachments=[stored.path]
    )

    async def read(db):
        run = await find_run_by_prefix(db, run_id)
        return run.task

    task = await service._read(read)
    assert "посмотри баг" in task
    assert stored.path in task
    assert "read_image" in task, "агенту сказано, чем это читать"


@pytest.mark.asyncio
async def test_path_outside_attachments_is_rejected(service) -> None:
    session = await service.create_session(title="чужое")
    with pytest.raises(AttachmentPathError):
        await service.send_message(
            session.session_id, "текст", None, attachments=["../../etc/passwd"]
        )


@pytest.mark.asyncio
async def test_missing_attachment_is_rejected(service) -> None:
    session = await service.create_session(title="нет файла")
    with pytest.raises(AttachmentPathError):
        await service.send_message(
            session.session_id, "текст", None, attachments=[".attachments/нет.png"]
        )
```

- [ ] **Шаг 2: убедиться, что падают**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_attachments.py -q -k attachment_paths`
Expected: FAIL — `send_message` не принимает `attachments`

- [ ] **Шаг 3: реализация**

В `attachments.py`:

```python
class AttachmentPathError(ValueError):
    """Путь вложения не из `.attachments/` этой сессии; наружу — HTTP 400."""


def attachments_note(paths: list[str]) -> str:
    """Строка, которой сообщение сообщает агенту о вложениях.

    Дописывается к тексту задачи и попадает в трассу — то есть в ленте
    видно ровно то, что получил агент, без скрытых добавок.
    """
    listed = ", ".join(paths)
    return f"Вложения (прочитай их read_image / read_document): {listed}"


def verify_attachment(workspace: Path, rel: str) -> Path:
    """Путь обязан лежать в `.attachments/` этой рабочей папки и существовать."""
    root = (workspace / ATTACHMENTS_DIR).resolve()
    candidate = (workspace / rel).resolve()
    if not candidate.is_relative_to(root):
        raise AttachmentPathError(f"вложение '{rel}' не из {ATTACHMENTS_DIR} этой сессии")
    if not candidate.is_file():
        raise AttachmentPathError(f"вложения '{rel}' нет на диске")
    return candidate
```

В `send_message` — новый параметр `attachments: Sequence[str] = ()`, и сразу после резолва workspace:

```python
        if attachments:
            for rel in attachments:
                verify_attachment(workspace, rel)  # AttachmentPathError → 400
            text = f"{text}\n\n{attachments_note(list(attachments))}"
```

`SendMessageRequest` получает `attachments: list[str] = []`; `api.py` передаёт его и маппит `AttachmentPathError` → 400.

- [ ] **Шаг 4: тесты зелёные и коммит**

```bash
COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_attachments.py -q
git add src tests && git commit -m "feat(gateway): вложения доезжают до агента строкой в задаче"
```

---

### Задача 9: интеграционная проверка — вложение не попадает в git

Отдельная задача, потому что именно ради этого свойства выбран
`.git/info/exclude`, а проверяется оно только сквозь весь путь.

**Files:**
- Test: `tests/test_gateway_attachments.py`

- [ ] **Шаг 1: тест**

```python
@pytest.mark.asyncio
async def test_attachment_leaves_working_tree_clean(service) -> None:
    """Скриншот не должен уехать в историю task-ветки автокоммитом (Flow C)."""
    repo = GitRepo(service.workspace)
    await repo.init()
    await repo.ensure_identity(name="тест", email="тест@example.com")

    session = await service.create_session(title="чистое дерево")
    await service.store_attachment(session.session_id, "скрин.png", b"\x89PNG")

    code, out, _ = await repo._git("status", "--porcelain")

    assert code == 0
    assert ".attachments" not in out, f"вложение видно git: {out!r}"
```

- [ ] **Шаг 2: прогнать**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_attachments.py -q -k working_tree`
Expected: PASS (реализация из задач 6-7 уже должна это обеспечивать; если FAIL — чинить `ensure_git_excluded`, а не тест)

- [ ] **Шаг 3: коммит**

```bash
git add tests && git commit -m "test(gateway): вложение не попадает в рабочее дерево git"
```

---

### Задача 10: клиентский API

**Files:**
- Modify: `web/src/api/types.ts`, `client.ts`, `test/fakeApi.ts`
- Test: `web/src/api/client.test.ts`

**Interfaces:**
- Produces: типы `ExecutorOption`, `SlashCommand`, `FileSuggestion`, `Attachment`; методы `executors()`, `commands()`, `sessionFiles(id, q)`, `uploadAttachment(id, file)`; `sendMessage(..., override?, attachments?)`.

- [ ] **Шаг 1: тест**

```ts
it("загружает вложение как multipart и возвращает путь", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        path: ".attachments/ab12_скрин.png",
        name: "скрин.png",
        size_bytes: 4,
        mime: "image/png",
        too_large_for_vision: false,
      }),
      { status: 201, headers: { "content-type": "application/json" } },
    ),
  );
  vi.stubGlobal("fetch", fetchMock);
  const api = createClient({ baseUrl: "" });

  const file = new File([new Uint8Array([1, 2, 3, 4])], "скрин.png", {
    type: "image/png",
  });
  const stored = await api.uploadAttachment("s1", file);

  expect(stored.path).toBe(".attachments/ab12_скрин.png");
  const init = fetchMock.mock.calls[0][1];
  expect(init.body).toBeInstanceOf(FormData);
  expect(init.headers?.["content-type"]).toBeUndefined();
});

it("передаёт пути вложений вместе с текстом", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ run_id: "r1", state: "running" }), {
      status: 201,
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  const api = createClient({ baseUrl: "" });

  await api.sendMessage("s1", "смотри", "yolo", {}, [".attachments/a_скрин.png"]);

  const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
  expect(body.attachments).toEqual([".attachments/a_скрин.png"]);
});
```

- [ ] **Шаг 2: убедиться, что падают**

Run: `npm --prefix web test -- client`
Expected: FAIL — нет `uploadAttachment`

- [ ] **Шаг 3: типы и клиент**

```ts
export interface ExecutorOption {
  value: string;
  kind: "native" | "external";
  adapter: string | null;
  available: boolean;
  is_active: boolean;
}

export interface SlashCommand {
  name: string;
  usage: string;
  help: string;
}

export interface FileSuggestion {
  path: string;
  label: string;
}

export interface Attachment {
  path: string;
  name: string;
  size_bytes: number;
  mime: string | null;
  too_large_for_vision: boolean;
}
```

```ts
    executors: () => request<ExecutorOption[]>("/executors"),
    commands: () => request<SlashCommand[]>("/commands"),
    sessionFiles: (sessionId, q) =>
      request<FileSuggestion[]>(
        `/sessions/${encodeURIComponent(sessionId)}/files?q=${encodeURIComponent(q)}`,
      ),
    uploadAttachment: (sessionId, file) => {
      const form = new FormData();
      form.append("file", file);
      // Заголовок content-type не ставим: браузер сам допишет boundary,
      // а ручной multipart/form-data без boundary сервер не разберёт.
      return request<Attachment>(
        `/sessions/${encodeURIComponent(sessionId)}/attachments`,
        { method: "POST", body: form },
      );
    },
```

`sendMessage` получает пятый аргумент `attachments?: string[]` и добавляет
`...(attachments?.length ? { attachments } : {})` в тело.

`request` должен пропускать `FormData` без `JSON.stringify` и без
`content-type` — проверить его текущую реализацию и, если он ставит
заголовок безусловно, сделать это условным.

В `fakeApi.ts` добавить заглушки всех четырёх методов.

- [ ] **Шаг 4: тесты зелёные и коммит**

```bash
npm --prefix web test && npm --prefix web run build
git add web/src && git commit -m "feat(web): клиент для команд, файлов и вложений"
```

---

### Задача 11: порт `detect_completion`

**Files:**
- Create: `web/src/model/completion.ts`, `web/src/model/completion.test.ts`

**Interfaces:**
- Produces: `CompletionMode`, `detectCompletion(textBeforeCursor: string): {mode: CompletionMode; token: string}`, `replaceToken(text: string, caret: number, value: string): {text: string; caret: number}`.

- [ ] **Шаг 1: тесты** (случаи повторяют `tests/test_chat_completion.py`)

```ts
import { describe, expect, it } from "vitest";

import { detectCompletion, replaceToken } from "./completion";

describe("detectCompletion", () => {
  it("молчит на обычном тексте", () => {
    expect(detectCompletion("привет как дела")).toEqual({
      mode: "idle",
      token: "",
    });
  });

  it("видит слэш-команду в начале строки", () => {
    expect(detectCompletion("/he")).toEqual({ mode: "slash", token: "/he" });
  });

  it("не считает слэш командой после пробела", () => {
    expect(detectCompletion("текст /he").mode).toBe("idle");
  });

  it("видит @ в середине строки", () => {
    expect(detectCompletion("посмотри @src/a")).toEqual({
      mode: "at",
      token: "@src/a",
    });
  });

  it("@ важнее слэша", () => {
    expect(detectCompletion("/cmd @fi").mode).toBe("at");
  });

  it("пустой ввод — покой", () => {
    expect(detectCompletion("")).toEqual({ mode: "idle", token: "" });
  });
});

describe("replaceToken", () => {
  it("заменяет токен под курсором и ставит курсор после вставки", () => {
    const result = replaceToken("смотри @sr", 10, "@src/app.tsx");
    expect(result.text).toBe("смотри @src/app.tsx ");
    expect(result.caret).toBe(result.text.length);
  });

  it("сохраняет хвост строки после курсора", () => {
    const result = replaceToken("смотри @sr конец", 10, "@src/app.tsx");
    expect(result.text).toBe("смотри @src/app.tsx  конец");
  });
});
```

- [ ] **Шаг 2: убедиться, что падают**

Run: `npm --prefix web test -- completion`
Expected: FAIL — модуль не найден

- [ ] **Шаг 3: реализация**

```ts
/**
 * Режим подсказок ввода — порт `cli/chat_completion.py: detect_completion`.
 *
 * Логика повторена в браузере, а не вынесена на сервер, потому что
 * определять режим надо на каждое нажатие клавиши: запрос по сети на
 * букву сделал бы поле ввода заметно медленнее набора.
 */
export type CompletionMode = "idle" | "slash" | "at";

export interface CompletionQuery {
  mode: CompletionMode;
  token: string;
}

export function detectCompletion(textBeforeCursor: string): CompletionQuery {
  const text = textBeforeCursor;
  if (text === "") return { mode: "idle", token: "" };

  // Токен от последнего пробела: приоритет у @, его можно писать после текста.
  let index = text.length - 1;
  while (index >= 0 && !" \t\n".includes(text[index])) index -= 1;
  const token = text.slice(index + 1);
  if (token.startsWith("@")) return { mode: "at", token };

  // Слэш-команда — только если строка начинается с / и курсор в первом токене.
  const stripped = text.replace(/^[ \t\n]+/, "");
  if (stripped.startsWith("/") && !text.includes("\n") && !stripped.includes(" ")) {
    return { mode: "slash", token: stripped };
  }
  return { mode: "idle", token: "" };
}

export function replaceToken(
  text: string,
  caret: number,
  value: string,
): { text: string; caret: number } {
  const before = text.slice(0, caret);
  const after = text.slice(caret);
  let index = before.length - 1;
  while (index >= 0 && !" \t\n".includes(before[index])) index -= 1;
  const head = before.slice(0, index + 1);
  // Пробел после вставки: следующее слово не слипнется с путём, и
  // detectCompletion сразу вернётся в idle — меню закроется само.
  const next = `${head}${value} `;
  return { text: `${next}${after}`, caret: next.length };
}
```

- [ ] **Шаг 4: тесты зелёные и коммит**

```bash
npm --prefix web test -- completion
git add web/src/model && git commit -m "feat(web): определение режима подсказок ввода"
```

---

### Задача 12: меню подсказок

**Files:**
- Create: `web/src/components/Completion.tsx`, `.css`, `.test.tsx`

**Interfaces:**
- Consumes: `CompletionMode` (задача 11), `SlashCommand`, `FileSuggestion` (задача 10).
- Produces: `<Completion items={...} active={...} onPick={...} />`, тип `CompletionItem = {value: string; label: string; description: string}`.

- [ ] **Шаг 1: тесты**

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Completion, type CompletionItem } from "./Completion";

const ITEMS: CompletionItem[] = [
  { value: "/help", label: "/help", description: "показать команды" },
  { value: "/new", label: "/new", description: "новый чат" },
];

describe("Completion", () => {
  it("показывает значение и описание", () => {
    render(<Completion items={ITEMS} active={0} onPick={vi.fn()} />);
    expect(screen.getByText("/help")).toBeInTheDocument();
    expect(screen.getByText("показать команды")).toBeInTheDocument();
  });

  it("отмечает активную строку для программы чтения с экрана", () => {
    render(<Completion items={ITEMS} active={1} onPick={vi.fn()} />);
    expect(screen.getByText("/new").closest("li")).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("возвращает выбранное по клику", async () => {
    const onPick = vi.fn();
    render(<Completion items={ITEMS} active={0} onPick={onPick} />);
    await userEvent.click(screen.getByText("/new"));
    expect(onPick).toHaveBeenCalledWith("/new");
  });

  it("ничего не рисует на пустом списке", () => {
    const { container } = render(
      <Completion items={[]} active={0} onPick={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
```

- [ ] **Шаг 2: убедиться, что падают**

Run: `npm --prefix web test -- Completion`
Expected: FAIL — модуль не найден

- [ ] **Шаг 3: компонент**

```tsx
import "./Completion.css";

export interface CompletionItem {
  value: string;
  label: string;
  description: string;
}

export function Completion({
  items,
  active,
  onPick,
}: {
  items: CompletionItem[];
  active: number;
  onPick: (value: string) => void;
}) {
  // Пустой список не рисуем вовсе: рамка без строк читается как поломка.
  if (items.length === 0) return null;
  return (
    <ul className="completion" role="listbox" aria-label="Подсказки ввода">
      {items.map((item, index) => (
        <li
          key={item.value}
          role="option"
          aria-selected={index === active}
          className={`completion__row${index === active ? " completion__row--active" : ""}`}
        >
          <button type="button" onClick={() => onPick(item.value)}>
            <span className="completion__label">{item.label}</span>
            <span className="completion__hint">{item.description}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}
```

- [ ] **Шаг 4: стили**

`Completion.css` по токенам `web/src/styles/tokens.css`, как у `ModelPicker.css`: тёмная подложка, акцент на активной строке, `max-height: 8 строк` со скроллом, `position: absolute` над полем ввода. На `@media (max-width: 899px)` — на всю ширину поля.

- [ ] **Шаг 5: тесты зелёные и коммит**

```bash
npm --prefix web test -- Completion && npm --prefix web run build
git add web/src/components && git commit -m "feat(web): меню подсказок ввода"
```

---

### Задача 13: чипы вложений

**Files:**
- Create: `web/src/components/Attachments.tsx`, `.css`, `.test.tsx`

**Interfaces:**
- Consumes: `Attachment` (задача 10).
- Produces: `<Attachments items={...} onRemove={...} />`.

- [ ] **Шаг 1: тесты**

```tsx
const IMAGE: Attachment = {
  path: ".attachments/ab_скрин.png",
  name: "скрин.png",
  size_bytes: 2048,
  mime: "image/png",
  too_large_for_vision: false,
};
const DOC: Attachment = {
  path: ".attachments/cd_отчёт.pdf",
  name: "отчёт.pdf",
  size_bytes: 100,
  mime: null,
  too_large_for_vision: false,
};

it("показывает исходное имя, а не имя на диске", () => {
  render(<Attachments items={[IMAGE]} onRemove={vi.fn()} />);
  expect(screen.getByText("скрин.png")).toBeInTheDocument();
  expect(screen.queryByText(/ab_/)).not.toBeInTheDocument();
});

it("убирает вложение крестиком", async () => {
  const onRemove = vi.fn();
  render(<Attachments items={[DOC]} onRemove={onRemove} />);
  await userEvent.click(screen.getByLabelText("Убрать отчёт.pdf"));
  expect(onRemove).toHaveBeenCalledWith(".attachments/cd_отчёт.pdf");
});

it("предупреждает, что картинка слишком велика для модели", () => {
  render(
    <Attachments
      items={[{ ...IMAGE, too_large_for_vision: true }]}
      onRemove={vi.fn()}
    />,
  );
  expect(screen.getByText(/модель не увидит/i)).toBeInTheDocument();
});

it("ничего не рисует без вложений", () => {
  const { container } = render(<Attachments items={[]} onRemove={vi.fn()} />);
  expect(container).toBeEmptyDOMElement();
});
```

- [ ] **Шаг 2: убедиться, что падают**

Run: `npm --prefix web test -- Attachments`
Expected: FAIL — модуль не найден

- [ ] **Шаг 3: компонент и стили**

Чип: для `mime`, начинающегося с `image/`, — миниатюра (`<img>` с
`src` на `/sessions/{id}/…`? нет — файл не раздаётся; вместо этого
показывается значок изображения и имя, миниатюра появится в ленте после
отправки, где путь уже известен серверу). Для остальных — значок
документа и имя. Крестик с `aria-label={`Убрать ${item.name}`}`.
Предупреждение `too_large_for_vision` — строкой под чипом: «файл больше
5 MB, модель не увидит его целиком; пригодится как исходник».

`Attachments.css` — горизонтальный ряд с переносом, по токенам.

- [ ] **Шаг 4: тесты зелёные и коммит**

```bash
npm --prefix web test -- Attachments && npm --prefix web run build
git add web/src/components && git commit -m "feat(web): чипы вложений над полем ввода"
```

---

### Задача 14: поле ввода собирается вместе

**Files:**
- Modify: `web/src/components/Composer.tsx` + `.css`
- Test: `web/src/components/Composer.test.tsx`

**Interfaces:**
- Consumes: `Completion` (12), `Attachments` (13), `detectCompletion`/`replaceToken` (11), `ExecutorOption` (10).
- Produces: `Composer` с пропсами `executors`, `commands`, `onFileQuery`, `attachments`, `onAttach`, `onRemoveAttachment`.

- [ ] **Шаг 1: тесты**

```tsx
it("показывает автономию сырыми значениями, как в настройках", () => {
  render(<Composer {...base} />);
  const select = screen.getByLabelText("Автономия");
  expect(within(select).getByRole("option", { name: "supervised" })).toBeInTheDocument();
  expect(screen.queryByText("под надзором")).not.toBeInTheDocument();
});

it("перечисляет исполнителей по адаптерам и гасит недоступные", () => {
  render(
    <Composer
      {...base}
      executors={[
        { value: "native", kind: "native", adapter: null, available: true, is_active: true },
        { value: "claude-code", kind: "external", adapter: "claude-code", available: true, is_active: false },
        { value: "codex", kind: "external", adapter: "codex", available: false, is_active: false },
      ]}
    />,
  );
  const select = screen.getByLabelText("Исполнитель");
  expect(within(select).getByRole("option", { name: "codex" })).toBeDisabled();
  expect(within(select).getByRole("option", { name: "claude-code" })).toBeEnabled();
});

it("показывает подсказки команд и вставляет выбранную", async () => {
  render(<Composer {...base} commands={[{ name: "help", usage: "/help", help: "показать команды" }]} />);
  const field = screen.getByLabelText("Написать Сварогу");

  await userEvent.type(field, "/he");

  expect(screen.getByText("показать команды")).toBeInTheDocument();
  await userEvent.keyboard("{Enter}");
  expect(field).toHaveValue("/help ");
});

it("Enter при открытом меню не отправляет сообщение", async () => {
  const onSend = vi.fn();
  render(<Composer {...base} onSend={onSend} commands={[{ name: "help", usage: "/help", help: "h" }]} />);

  await userEvent.type(screen.getByLabelText("Написать Сварогу"), "/he{Enter}");

  expect(onSend).not.toHaveBeenCalled();
});

it("Escape закрывает меню, а следующий Enter отправляет", async () => {
  const onSend = vi.fn();
  render(<Composer {...base} onSend={onSend} commands={[{ name: "help", usage: "/help", help: "h" }]} />);

  await userEvent.type(screen.getByLabelText("Написать Сварогу"), "/he{Escape}{Enter}");

  expect(onSend).toHaveBeenCalledWith("/he", []);
});

it("вставка картинки из буфера прикрепляет её", async () => {
  const onAttach = vi.fn();
  render(<Composer {...base} onAttach={onAttach} />);
  const field = screen.getByLabelText("Написать Сварогу");
  const file = new File([new Uint8Array([1])], "скрин.png", { type: "image/png" });

  fireEvent.paste(field, { clipboardData: { files: [file], items: [] } });

  expect(onAttach).toHaveBeenCalledWith(file);
});
```

- [ ] **Шаг 2: убедиться, что падают**

Run: `npm --prefix web test -- Composer`
Expected: FAIL — нет подсказок и вложений

- [ ] **Шаг 3: подписи**

Удалить `AUTONOMY_LABELS` и `EXECUTOR_LABELS` из `types.ts` и их
использование: автономия рисует `mode` как есть, исполнитель — `option.value`
с `disabled={!option.available}` и
`title={option.available ? undefined : "CLI этого агента не найден в PATH"}`.

- [ ] **Шаг 4: подсказки**

Состояние: `const [query, setQuery] = useState<CompletionQuery>({mode: "idle", token: ""})`,
`const [active, setActive] = useState(0)`, `const [dismissed, setDismissed] = useState(false)`.

На каждое изменение поля и перемещение курсора вызывать `detectCompletion(value.slice(0, caret))`
и сбрасывать `dismissed`. Элементы меню:

```tsx
const items: CompletionItem[] =
  dismissed || query.mode === "idle"
    ? []
    : query.mode === "slash"
      ? commands
          .filter((c) => `/${c.name}`.startsWith(query.token))
          .map((c) => ({ value: `/${c.name}`, label: `/${c.name}`, description: c.help }))
      : files.map((f) => ({ value: `@${f.path}`, label: f.path, description: "файл" }));
```

В `onKeyDown`, **до** ветки отправки:

```tsx
if (items.length > 0) {
  if (event.key === "ArrowDown") {
    event.preventDefault();
    setActive((i) => (i + 1) % items.length);
    return;
  }
  if (event.key === "ArrowUp") {
    event.preventDefault();
    setActive((i) => (i - 1 + items.length) % items.length);
    return;
  }
  if (event.key === "Escape") {
    event.preventDefault();
    setDismissed(true);
    return;
  }
  if (event.key === "Enter" || event.key === "Tab") {
    // Пока меню открыто, Enter вставляет подсказку, а не отправляет:
    // иначе первое же дополнение улетит агенту недописанным.
    event.preventDefault();
    pick(items[active].value);
    return;
  }
}
```

`pick` использует `replaceToken` и возвращает курсор через `setSelectionRange`.

- [ ] **Шаг 5: вложения**

Кнопка-скрепка рядом с микрофоном (`aria-label="Прикрепить файл"`,
скрытый `<input type="file" multiple>`), `onPaste` и `onDrop` на поле —
все три зовут `onAttach(file)` для каждого файла. Над полем — `<Attachments>`.
`onSend` получает вторым аргументом список путей вложений.

- [ ] **Шаг 6: тесты зелёные и коммит**

```bash
npm --prefix web test && npm --prefix web run build
git add web/src && git commit -m "feat(web): подсказки, вложения и точные подписи в поле ввода"
```

---

### Задача 15: экран диалога — команды, загрузка, миниатюры

**Files:**
- Modify: `web/src/screens/ChatScreen.tsx`, `web/src/model/thread.ts`, `web/src/screens/ChatScreen.css`
- Test: `web/src/screens/ChatScreen.test.tsx`

**Interfaces:**
- Consumes: всё из задач 10-14.

- [ ] **Шаг 1: тесты**

```tsx
it("/new заводит новый чат, а не уходит агенту", async () => {
  const sendMessage = vi.fn();
  const onNew = vi.fn();
  render(<ChatScreen {...base} api={fakeApi({ sendMessage })} onNew={onNew} />);

  await userEvent.type(screen.getByLabelText("Написать Сварогу"), "/new");
  await userEvent.keyboard("{Enter}{Enter}");

  expect(onNew).toHaveBeenCalled();
  expect(sendMessage).not.toHaveBeenCalled();
});

it("неизвестная команда не отправляется, а объясняется", async () => {
  const sendMessage = vi.fn();
  render(<ChatScreen {...base} api={fakeApi({ sendMessage })} />);

  await userEvent.type(screen.getByLabelText("Написать Сварогу"), "/опечатка{Escape}{Enter}");

  expect(sendMessage).not.toHaveBeenCalled();
  expect(await screen.findByText(/неизвестная команда/i)).toBeInTheDocument();
});

it("прикреплённый файл уходит вместе с сообщением", async () => {
  const sendMessage = vi.fn().mockResolvedValue({ run_id: "r1", state: "running" });
  const uploadAttachment = vi.fn().mockResolvedValue({
    path: ".attachments/ab_скрин.png",
    name: "скрин.png",
    size_bytes: 4,
    mime: "image/png",
    too_large_for_vision: false,
  });
  render(<ChatScreen {...base} api={fakeApi({ sendMessage, uploadAttachment })} sessionId="s1" />);

  const file = new File([new Uint8Array([1])], "скрин.png", { type: "image/png" });
  fireEvent.paste(screen.getByLabelText("Написать Сварогу"), {
    clipboardData: { files: [file], items: [] },
  });
  await screen.findByText("скрин.png");
  await userEvent.type(screen.getByLabelText("Написать Сварогу"), "смотри{Enter}");

  expect(sendMessage).toHaveBeenCalledWith(
    "s1",
    "смотри",
    expect.anything(),
    expect.anything(),
    [".attachments/ab_скрин.png"],
  );
});

it("ошибка загрузки показана и не мешает отправить сообщение", async () => {
  const uploadAttachment = vi.fn().mockRejectedValue(new ApiError("расширение '.exe' не поддержано", 415));
  const sendMessage = vi.fn().mockResolvedValue({ run_id: "r1", state: "running" });
  render(<ChatScreen {...base} api={fakeApi({ uploadAttachment, sendMessage })} sessionId="s1" />);

  fireEvent.paste(screen.getByLabelText("Написать Сварогу"), {
    clipboardData: { files: [new File([], "вирус.exe")], items: [] },
  });
  expect(await screen.findByText(/не поддержано/)).toBeInTheDocument();

  await userEvent.type(screen.getByLabelText("Написать Сварогу"), "текст{Enter}");
  expect(sendMessage).toHaveBeenCalledWith("s1", "текст", expect.anything(), expect.anything(), []);
});

it("вложение в ленте рисуется миниатюрой, а не строкой пути", () => {
  render(<ChatScreen {...base} api={fakeApi({
    sessionThread: vi.fn().mockResolvedValue({
      session_id: "s1",
      title: "",
      items: [{ kind: "user", text: "смотри\n\nВложения (прочитай их read_image / read_document): .attachments/ab_скрин.png" }],
    }),
  })} sessionId="s1" />);

  return screen.findByRole("img", { name: /скрин\.png/ });
});
```

- [ ] **Шаг 2: убедиться, что падают**

Run: `npm --prefix web test -- ChatScreen`
Expected: FAIL — команды уходят агенту

- [ ] **Шаг 3: команды**

В `send` перед вызовом API:

```tsx
      const parsed = parseCommand(text);
      if (parsed !== null) {
        runCommand(parsed);  // /help /new /sessions /executor /policies /copy
        return;
      }
```

`parseCommand` — маленькая функция в `web/src/model/completion.ts`:
принимает текст, возвращает `{name, args} | null`; неизвестная команда
возвращает `{name: "", args: сырое_имя}`, и `runCommand` показывает
«неизвестная команда: /опечатка» строкой `status` в ленте, а не отправляет
её агенту.

Действия: `/help` — `status`-элемент со списком из `api.commands()`;
`/new` — проп `onNew` (уже есть в `App`); `/sessions` — проп `onSessions`,
переводящий фокус на навигатор; `/executor` и `/policies` — фокус на
соответствующий селект (`ref`); `/copy` — `navigator.clipboard.writeText`
последнего `say`-элемента.

- [ ] **Шаг 4: загрузка**

```tsx
  const attach = useCallback(
    async (file: File) => {
      setUploadError(null);
      try {
        const target = sessionId ?? (await ensureSession());
        const stored = await api.uploadAttachment(target, file);
        setAttachments((current) => [...current, stored]);
      } catch (exc: unknown) {
        setUploadError(
          exc instanceof ApiError ? exc.message : "Не удалось загрузить файл.",
        );
      }
    },
    [api, sessionId, ensureSession],
  );
```

После успешной отправки — `setAttachments([])`.

- [ ] **Шаг 5: миниатюра в ленте**

В `thread.ts` при разборе `user`-элемента отделять строку вложений
(она начинается с `Вложения (`) в поле `attachments: string[]`, оставляя
в `text` только написанное человеком. `ChatScreen` рисует картинки
`<img src={`${baseUrl}/sessions/${sessionId}/attachments/${encodeURIComponent(name)}`} alt={name} />`.

Это требует **раздачи вложения**: добавить в `api.py`

```python
    @app.get("/sessions/{session_id}/attachments/{name}")
    async def read_attachment(session_id: str, name: str, service: ServiceDep) -> FileResponse:
        try:
            path = await service.attachment_path(session_id, name)
        except (SessionNotFoundError, AttachmentPathError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return FileResponse(path)
```

и `GatewayService.attachment_path(session_id, name)`, который резолвит
`.attachments/<name>` через `verify_attachment` (задача 8) — тот же
fail-closed резолв, что и при отправке.

- [ ] **Шаг 6: тесты зелёные и коммит**

```bash
COLUMNS=200 .venv/bin/python -m pytest -q && npm --prefix web test && npm --prefix web run build
git add src web tests && git commit -m "feat(web): команды, загрузка и миниатюры вложений в диалоге"
```

---

### Задача 16: документация и живая проверка

**Files:**
- Modify: `docs/superpowers/specs/2026-07-27-web-ui-gorn-design.md`

- [ ] **Шаг 1: спек**

В «Состояние реализации» и список дельты API добавить `GET /executors`,
`GET /commands`, `GET /sessions/{id}/files`, `POST` и `GET`
`/sessions/{id}/attachments`, поля `adapter` и `attachments` у сообщения.

- [ ] **Шаг 2: все гейты**

```bash
COLUMNS=200 .venv/bin/python -m pytest -q \
  && .venv/bin/ruff check src tests && .venv/bin/ruff format --check . \
  && .venv/bin/mypy \
  && npm --prefix web test && npm --prefix web run build
```

- [ ] **Шаг 3: живая проверка**

Поднять `svarog serve` из проектной папки (не из `agent-home`) и пройти:
набрать `/`, увидеть шесть команд; выбрать `/help`; набрать `@` и увидеть
файлы workspace без `node_modules` и `.attachments`; вставить скриншот из
буфера, увидеть чип, отправить, увидеть миниатюру в ленте; проверить
`git status` — чисто; переключить исполнителя на `codex` без CLI и увидеть
заблокированный пункт.

- [ ] **Шаг 4: коммит**

```bash
git add docs && git commit -m "docs: команды, @-файлы и вложения в поле ввода"
```

---

## Самопроверка плана

**Покрытие спека.** §1 автономия → задача 14 шаг 3. §2 адаптеры →
задачи 1-3 (модель), 14 шаг 3 (селект). §3 команды → 4 (реестр), 11
(режим), 12 (меню), 14 (вживление), 15 (действия); файлы → 5, 12, 14.
§4 вложения → 6 (git), 7 (приём), 8 (доставка агенту), 9 (интеграционная
проверка чистого дерева), 13 (чипы), 15 (загрузка и миниатюры). Таблица
ошибок разложена: 422 — задача 2; 415/413/400/409/404 — задача 7; 400 на
чужой путь — задача 8.

**Расхождение со спеком, зафиксированное осознанно.** Спек описывает чип
вложения как миниатюру уже в композере. План рисует миниатюру только в
ленте, а в композере — значок и имя: до отправки файл лежит на сервере, и
показывать его в композере значило бы либо раздавать вложения ещё до
сообщения, либо читать файл в браузере вторым способом. Раздача добавлена
в задаче 15 ради ленты; переносить её в композер — лишняя связность ради
косметики.

**Заглушек нет.** Все шаги несут код или точную инструкцию. Единственное
место, где код не приведён дословно, — стили (`Completion.css`,
`Attachments.css`): там названы токены, размеры и брейкпоинт, а верстка
повторяет уже существующий `ModelPicker.css`.

**Согласованность имён.** `ExecutorOption`/`ExecutorOptionView`,
`SlashCommand`/`SlashCommandView`, `FileSuggestion`/`FileSuggestionView`,
`StoredAttachment`/`AttachmentView` — дата-класс на сервере, модель
ответа рядом, одноимённый интерфейс на клиенте. `detectCompletion` и
`replaceToken` объявлены в задаче 11 и используются в 14 с теми же
сигнатурами. `verify_attachment` объявлена в задаче 8 и переиспользована
в 15. `attachments_note` — единственное место, где формируется строка
вложений, и `thread.ts` в задаче 15 разбирает ровно её префикс.
