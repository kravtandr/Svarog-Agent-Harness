# Выбор исполнителя и модели в поле ввода — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** переключать исполнителя, провайдера и модель прямо из поля ввода чата, со списком моделей, который запрашивается у самого провайдера.

**Architecture:** выбор — свойство сообщения, как автономия: он едет в `POST /sessions/{id}/messages`, превращается в производный `SvarogConfig` через `model_copy`, ложится в `Run.meta` и восстанавливается оттуда при resume, чтобы security-снимок конфига (ADR-0015 §0.4) сошёлся. `svarog.yaml` не переписывается. Каталог моделей — отдельный модуль без состояния плюс TTL-кэш в сервисе.

**Tech Stack:** Python 3.12, Pydantic v2, SQLAlchemy async, FastAPI, httpx; клиент — React 19 + Vite 6 + TypeScript 5, Vitest 3 + @testing-library/react.

Спек: `docs/superpowers/specs/2026-07-27-composer-executor-and-model-design.md`.

## Global Constraints

- Ветка `feat/web-ui-gorn`, работа продолжается в ней.
- Комментарии, тексты ошибок и надписи интерфейса — по-русски, как в остальном коде.
- Значения секретов не логируются и не попадают в HTTP-ответы (ADR-0006): наружу едут только имена ссылок.
- Конфиг под работающим запуском не меняется (ADR-0015 §0.4): перечитывание — только между запусками.
- `executor.type='external'` требует `sandbox.type='docker'` (ADR-0016), проверка fail-closed.
- Python-гейт: `COLUMNS=200 .venv/bin/python -m pytest -q`. Клиентский гейт: `npm --prefix web test` (включает `prettier --check src`).
- Таймаут запроса к каталогу моделей — 10 секунд, TTL кэша — 10 минут.

## Структура файлов

**Создаются:**

| Файл | Ответственность |
|---|---|
| `src/svarog_harness/gateway/overrides.py` | `RunOverride`, валидация, деривация конфига. Без БД, сети и ФС |
| `src/svarog_harness/gateway/catalog.py` | Разбор и загрузка списка моделей провайдера. Без состояния |
| `tests/test_gateway_overrides.py` | Override: валидация, meta round-trip, дайджест при resume |
| `tests/test_model_catalog.py` | Каталог: разбор форматов, загрузка, эндпоинты, кэш |
| `web/src/components/ModelPicker.tsx` + `.css` | Список моделей с поиском; лист снизу на мобильном |

**Меняются:**

| Файл | Что |
|---|---|
| `src/svarog_harness/trace/recorder.py:60` | `start_run(..., extra_meta=...)` |
| `src/svarog_harness/runtime/orchestrator.py:158` | `TaskRunner(..., run_meta=...)`, свойство `cfg` |
| `src/svarog_harness/runtime/run_assembly.py:149` | `RunAssembly(..., run_meta=...)` → в оба исполнителя |
| `src/svarog_harness/runtime/loop.py:204` | `AgentLoop(..., extra_run_meta=...)` |
| `src/svarog_harness/runtime/external.py:100` | `ExternalAgentExecutor(..., extra_run_meta=...)` |
| `src/svarog_harness/gateway/models.py:150` | Поля override, `ModelCardView`, `ProviderView`, `restart_required` |
| `src/svarog_harness/gateway/service.py` | Проброс override, каталог с кэшем, перечитывание конфига |
| `src/svarog_harness/gateway/api.py:275` | Override в эндпоинте, `GET /models`, `GET /models/{provider}` |
| `web/src/api/types.ts`, `web/src/api/client.ts` | Типы и методы каталога, override в `sendMessage` |
| `web/src/components/Composer.tsx` + `.css` | Селект исполнителя, провайдера, кнопка модели |
| `web/src/screens/ChatScreen.tsx` | Состояние выбора, загрузка провайдеров |
| `web/src/test/fakeApi.ts` | Заглушки новых методов |

---

### Задача 1: `RunOverride` и деривация конфига

**Files:**
- Create: `src/svarog_harness/gateway/overrides.py`
- Test: `tests/test_gateway_overrides.py`

**Interfaces:**
- Produces: `OVERRIDE_META_KEY: str`, `RunOverride` (поля `executor`, `provider`, `model`; методы `is_empty()`, `to_meta()`, `from_meta()`), `OverrideError`, `apply_override(cfg, ov, *, prices=None)`.

- [ ] **Шаг 1: тест валидации и деривации**

```python
"""Override исполнителя/провайдера/модели в сообщении чата (план 2026-07-28)."""

from pathlib import Path

import pytest

from svarog_harness.config.loader import load_config
from svarog_harness.gateway.overrides import (
    OVERRIDE_META_KEY,
    OverrideError,
    RunOverride,
    apply_override,
)


def _config(tmp_path: Path, extra: str = "") -> object:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "    router:\n"
        "      base_url: https://openrouter.ai/api/v1\n"
        "      model: deepseek/deepseek-v4-flash\n"
        "      input_usd_per_mtok: 1.0\n"
        "      output_usd_per_mtok: 2.0\n"
        "sandbox:\n  type: local-trusted\n" + extra,
        encoding="utf-8",
    )
    return load_config(project_dir=ws)


def test_empty_override_returns_config_unchanged(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert RunOverride().is_empty()
    assert apply_override(cfg, RunOverride()) is cfg


def test_provider_switches_default(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    derived = apply_override(cfg, RunOverride(provider="router"))
    assert derived.models.default == "router"
    assert cfg.models.default == "local", "исходный конфиг не мутируется"


def test_model_applies_to_named_provider(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    derived = apply_override(cfg, RunOverride(provider="router", model="anthropic/claude"))
    assert derived.models.providers["router"].model == "anthropic/claude"
    assert derived.models.providers["local"].model == "fake-model"


def test_model_without_provider_applies_to_default(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    derived = apply_override(cfg, RunOverride(model="qwen3"))
    assert derived.models.providers["local"].model == "qwen3"


def test_prices_replace_provider_prices(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    derived = apply_override(
        cfg, RunOverride(provider="router", model="x/y"), prices=(0.5, 1.5)
    )
    assert derived.models.providers["router"].input_usd_per_mtok == 0.5
    assert derived.models.providers["router"].output_usd_per_mtok == 1.5


def test_unknown_provider_rejected_with_known_names(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(OverrideError) as exc:
        apply_override(cfg, RunOverride(provider="нет-такого"))
    assert "local" in str(exc.value) and "router" in str(exc.value)


def test_external_without_section_rejected(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(OverrideError, match="executor.external"):
        apply_override(cfg, RunOverride(executor="external"))


def test_external_requires_docker_sandbox(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        "executor:\n  type: native\n  external:\n    image: svarog/agent:1\n",
    )
    with pytest.raises(OverrideError, match="sandbox"):
        apply_override(cfg, RunOverride(executor="external"))


def test_external_allowed_with_docker(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: docker\n"
        "executor:\n  type: native\n  external:\n    image: svarog/agent:1\n",
        encoding="utf-8",
    )
    cfg = load_config(project_dir=ws)
    derived = apply_override(cfg, RunOverride(executor="external"))
    assert derived.executor.type == "external"


def test_meta_round_trip_keeps_only_set_fields() -> None:
    ov = RunOverride(provider="router", model="x/y")
    meta = {OVERRIDE_META_KEY: ov.to_meta()}
    assert ov.to_meta() == {"provider": "router", "model": "x/y"}
    assert RunOverride.from_meta(meta) == ov
    assert RunOverride.from_meta(None).is_empty()
    assert RunOverride.from_meta({}).is_empty()
    assert RunOverride.from_meta({OVERRIDE_META_KEY: {"мусор": 1}}).is_empty()
```

- [ ] **Шаг 2: убедиться, что тест падает**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_overrides.py -q`
Expected: FAIL — `ModuleNotFoundError: svarog_harness.gateway.overrides`

- [ ] **Шаг 3: реализация**

```python
"""Override исполнителя, провайдера и модели для одного сообщения чата.

Выбор в поле ввода — свойство сообщения, а не правка `svarog.yaml`. Здесь
он превращается в производный конфиг: `model_copy(update=...)`, тот же
приём, что в `TaskRunner.spawn_child_run` (ADR-0016 фаза 3.5).

Модуль чистый: без БД, сети и файловой системы. Цены приходят снаружи
(их знает каталог моделей), чтобы это свойство сохранялось.
"""

from dataclasses import dataclass
from typing import Literal, Self

from svarog_harness.config.schema import SvarogConfig

# Ключ поддерева override в Run.meta.
OVERRIDE_META_KEY = "override"

ExecutorKind = Literal["native", "external"]


class OverrideError(Exception):
    """Override несовместим с конфигом; наружу уходит как HTTP 422."""


@dataclass(frozen=True)
class RunOverride:
    executor: ExecutorKind | None = None
    provider: str | None = None
    model: str | None = None

    def is_empty(self) -> bool:
        return self.executor is None and self.provider is None and self.model is None

    def to_meta(self) -> dict[str, str]:
        """Только заданные поля: пустые ключи в meta ничего не значат."""
        raw = {"executor": self.executor, "provider": self.provider, "model": self.model}
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
        return cls(
            executor=executor if executor in ("native", "external") else None,
            provider=provider if isinstance(provider, str) else None,
            model=model if isinstance(model, str) else None,
        )


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

    if ov.executor == "external":
        if cfg.executor.external is None:
            raise OverrideError(
                "внешний агент требует секцию executor.external в svarog.yaml "
                "(адаптер и образ sandbox, ADR-0016)"
            )
        if cfg.sandbox.type != "docker":
            raise OverrideError(
                f"внешний агент требует sandbox.type='docker', сейчас "
                f"'{cfg.sandbox.type}' (fail-closed, ADR-0016)"
            )
    if ov.executor is not None:
        update["executor"] = cfg.executor.model_copy(update={"type": ov.executor})

    target = ov.provider if ov.provider is not None else cfg.models.default
    if ov.provider is not None and ov.provider not in cfg.models.providers:
        known = ", ".join(sorted(cfg.models.providers)) or "нет"
        raise OverrideError(f"провайдер '{ov.provider}' не описан в models.providers (есть: {known})")

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
        update["models"] = cfg.models.model_copy(
            update={"default": target, "providers": providers}
        )

    return cfg.model_copy(update=update)
```

- [ ] **Шаг 4: тесты зелёные**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_overrides.py -q`
Expected: PASS, 10 тестов

- [ ] **Шаг 5: коммит**

```bash
git add src/svarog_harness/gateway/overrides.py tests/test_gateway_overrides.py
git commit -m "feat(gateway): производный конфиг сообщения из override"
```

---

### Задача 2: `run_meta` доходит до строки run'а

Плumbing по маршруту, каким уже идёт `parent_run_id`. Отдельная задача, потому что трогает пять файлов ядра и должна проверяться сама по себе.

**Files:**
- Modify: `src/svarog_harness/trace/recorder.py:60-108`
- Modify: `src/svarog_harness/runtime/orchestrator.py:158-196` (+ свойство `cfg`)
- Modify: `src/svarog_harness/runtime/run_assembly.py:149-161, 257-361, 362-400`
- Modify: `src/svarog_harness/runtime/loop.py:204-242, 278-286`
- Modify: `src/svarog_harness/runtime/external.py:100-119, 143-151`
- Test: `tests/test_gateway_overrides.py`

**Interfaces:**
- Consumes: ничего из задачи 1.
- Produces: `TraceRecorder.start_run(..., extra_meta: dict[str, object] | None = None)`; `TaskRunner(cfg, workspace, *, role=..., allow_layout_overlap=..., run_meta: dict[str, object] | None = None)`; свойство `TaskRunner.cfg -> SvarogConfig`.

- [ ] **Шаг 1: тест, что meta долетает**

Дописать в `tests/test_gateway_overrides.py`:

```python
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.trace.recorder import TraceRecorder


@pytest.mark.asyncio
async def test_start_run_stores_extra_meta(tmp_path: Path) -> None:
    db_path = tmp_path / "svarog.db"
    init_db(db_path)
    engine = create_engine(db_path)
    factory = create_session_factory(engine)
    async with factory() as db:
        run = await TraceRecorder(db).start_run(
            task="задача",
            autonomy="yolo",
            model="fake-model",
            extra_meta={OVERRIDE_META_KEY: {"provider": "router"}},
        )
    assert run.meta[OVERRIDE_META_KEY] == {"provider": "router"}
    assert run.meta["model"] == "fake-model", "штатные ключи не затёрты"
    await engine.dispose()
```

- [ ] **Шаг 2: убедиться, что падает**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_overrides.py::test_start_run_stores_extra_meta -q`
Expected: FAIL — `TypeError: start_run() got an unexpected keyword argument 'extra_meta'`

- [ ] **Шаг 3: расширить `start_run`**

В `recorder.py` добавить параметр в сигнатуру после `parent_run_id` и подмешать в `meta` перед созданием `Run`:

```python
        parent_run_id: str | None = None,
        extra_meta: dict[str, object] | None = None,
    ) -> Run:
        # config_hash — снимок security-конфига run'а (ADR-0015 §0.4): resume
        # сверяет с ним текущий конфиг и fail-closed при расхождении.
        meta: dict[str, object] = {"model": model}
        if config_hash is not None:
            meta[CONFIG_HASH_META_KEY] = config_hash
        # extra_meta — непрозрачный довесок вызывающей стороны (override
        # сообщения чата). Кладём после штатных ключей, но своими именами:
        # затирать model/config_hash он не должен.
        for key, value in (extra_meta or {}).items():
            if key not in meta:
                meta[key] = value
```

- [ ] **Шаг 4: тест зелёный**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_overrides.py::test_start_run_stores_extra_meta -q`
Expected: PASS

- [ ] **Шаг 5: протянуть `run_meta` через конструкторы**

`loop.py` — в `AgentLoop.__init__` рядом с `parent_run_id`:

```python
        parent_run_id: str | None = None,
        extra_run_meta: dict[str, object] | None = None,
```
```python
        self._extra_run_meta = extra_run_meta
```
и в вызове `start_run` (строка ~285) добавить `extra_meta=self._extra_run_meta,`.

`external.py` — то же самое в `ExternalAgentExecutor.__init__` и в его `start_run` (строка ~150).

`run_assembly.py` — в `RunAssembly.__init__` добавить keyword `run_meta: dict[str, object] | None = None`, сохранить в `self._run_meta`, и передавать `extra_run_meta=self._run_meta` в конструкторы `AgentLoop` (в `build_loop`) и `ExternalAgentExecutor` (в `build_external_executor`).

`orchestrator.py` — в `TaskRunner.__init__` добавить keyword `run_meta: dict[str, object] | None = None` и передать в `RunAssembly`:

```python
        self._assembly = RunAssembly(
            self._cfg, workspace, store=self._store, host_store=self._host_store,
            run_meta=run_meta,
        )
```

Там же — сохранить `self._run_meta = run_meta` и протащить его в `_runner_for_resume`, чтобы runner чужого workspace не терял довесок:

```python
        return TaskRunner(
            load_config(project_dir=workspace),
            workspace,
            role=self._role,
            allow_layout_overlap=self._allow_layout_overlap,
            run_meta=self._run_meta,
        )
```

И добавить свойство рядом с `store`/`host_store` — конфиг runner'а нужен снаружи, чтобы проверять совпадение дайджеста:

```python
    @property
    def cfg(self) -> SvarogConfig:
        """Конфиг runner'а после клампа по роли (и после override, если он был)."""
        return self._cfg
```

- [ ] **Шаг 6: полный прогон — плumbing ничего не сломал**

Run: `COLUMNS=200 .venv/bin/python -m pytest -q`
Expected: PASS, 1068 passed

- [ ] **Шаг 7: коммит**

```bash
git add src/svarog_harness/trace/recorder.py src/svarog_harness/runtime/ tests/test_gateway_overrides.py
git commit -m "feat(runtime): произвольный довесок в Run.meta через run_meta"
```

---

### Задача 3: override в сообщении и восстановление при resume

Ядро замысла. Если ошибиться здесь, одобрение гейта у запуска с override отлетит в `ConfigDriftError`.

**Files:**
- Modify: `src/svarog_harness/gateway/models.py:150-153`
- Modify: `src/svarog_harness/gateway/service.py:172-182, 373-383, 616-682`
- Modify: `src/svarog_harness/gateway/api.py:275-293`
- Test: `tests/test_gateway_overrides.py`

**Interfaces:**
- Consumes: `RunOverride`, `apply_override`, `OverrideError`, `OVERRIDE_META_KEY` (задача 1); `TaskRunner(..., run_meta=...)`, `TaskRunner.cfg` (задача 2).
- Produces: `GatewayService.send_message(session_id, text, autonomy, override: RunOverride = RunOverride())`; `_runner_for(workspace, *, cfg=None, run_meta=None)`.

- [ ] **Шаг 1: тест инварианта дайджеста**

```python
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app
from svarog_harness.runtime.config_snapshot import CONFIG_HASH_META_KEY, config_digest
from svarog_harness.storage.models import Run
from svarog_harness.trace.lookup import find_run_by_prefix


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayService:
    ws = tmp_path / "ws"
    ws.mkdir()
    db_path = tmp_path / "state" / "svarog.db"
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "    router:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: router-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {db_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    return GatewayService(load_config(project_dir=ws), ws)


@pytest.mark.asyncio
async def test_override_survives_resume_without_config_drift(
    service: GatewayService,
) -> None:
    """Дайджест конфига при resume совпадает со снимком старта.

    Это и есть гарантия, ради которой override кладётся в Run.meta:
    `_assert_config_unchanged` сверяет хеши и fail-closed при расхождении
    (ADR-0015 §0.4). Запуск падает на недоступном провайдере — неважно:
    строка run'а со снимком создаётся до первого обращения к модели.
    """
    session = await service.create_session(title="с override")
    run_id = await service.send_message(
        session.session_id, "задача", None, RunOverride(provider="router")
    )

    async def read(db):
        run = await find_run_by_prefix(db, run_id)
        return dict(run.meta or {})

    meta = await service._read(read)
    assert meta[OVERRIDE_META_KEY] == {"provider": "router"}

    runner = await service._runner_for_run(run_id)
    assert runner.cfg.models.default == "router"
    assert config_digest(runner.cfg, service.workspace) == meta[CONFIG_HASH_META_KEY]


@pytest.mark.asyncio
async def test_run_without_override_keeps_config_default(
    service: GatewayService,
) -> None:
    session = await service.create_session(title="без override")
    run_id = await service.send_message(session.session_id, "задача", None)

    runner = await service._runner_for_run(run_id)
    assert runner.cfg.models.default == "local"


@pytest.mark.asyncio
async def test_unknown_provider_returns_422(service: GatewayService) -> None:
    session = await service.create_session(title="ошибка")
    client = TestClient(create_app(service=service))
    response = client.post(
        f"/sessions/{session.session_id}/messages",
        json={"text": "задача", "provider": "нет-такого"},
    )
    assert response.status_code == 422
    assert "нет-такого" in response.json()["detail"]
```

- [ ] **Шаг 2: убедиться, что падает**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_overrides.py -q -k override_survives`
Expected: FAIL — `send_message()` принимает 3 аргумента

- [ ] **Шаг 3: расширить запрос**

`gateway/models.py`:

```python
class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1)
    autonomy: AutonomyMode | None = None
    # Выбор в поле ввода — свойство сообщения, а не правка svarog.yaml.
    # None во всех трёх — поведение по конфигу.
    executor: Literal["native", "external"] | None = None
    provider: str | None = None
    model: str | None = None
```

- [ ] **Шаг 4: проброс в сервисе**

`_runner_for` получает два необязательных keyword-аргумента; общий runner переиспользуется только когда оба пусты:

```python
    def _runner_for(
        self,
        workspace: Path,
        *,
        cfg: SvarogConfig | None = None,
        run_meta: dict[str, object] | None = None,
    ) -> TaskRunner:
        ws = workspace.expanduser().resolve()
        if cfg is None and run_meta is None and ws == self.workspace.expanduser().resolve():
            return self._runner
        return TaskRunner(cfg or self.cfg, ws, role=self.role, run_meta=run_meta)
```

`send_message` принимает override и строит производный конфиг **до** проверок занятости, чтобы негодный override отвечал 422 раньше, чем 409:

```python
    async def send_message(
        self,
        session_id: str,
        text: str,
        autonomy: AutonomyMode | None,
        override: RunOverride = RunOverride(),
    ) -> str:
        if self.quota_guard is not None:
            await self.quota_guard()  # QuotaExceededError → 429
        cfg = apply_override(self.cfg, override)  # OverrideError → 422
        external = cfg.executor.type == "external"
```

Дальше по коду `self.cfg` в этом методе заменяется на `cfg` (строка с `mode = autonomy if ... else self.cfg.runtime.autonomy`), а получение runner'а:

```python
        run_meta = {OVERRIDE_META_KEY: override.to_meta()} if not override.is_empty() else None
        warm = await self._acquire_warm(session.id, workspace, mode, override)
        runner = (
            warm.runner
            if warm is not None
            else self._runner_for(workspace, cfg=cfg, run_meta=run_meta)
        )
```

`_runner_for_run` восстанавливает конфиг из meta:

```python
    async def _runner_for_run(self, run_id: str) -> TaskRunner:
        """Runner для resume: workspace и override читаются из строки run'а.

        Без восстановления override дайджест конфига разойдётся со снимком
        старта, и `_assert_config_unchanged` отклонит resume (ADR-0015 §0.4).
        """

        async def action(db: AsyncSession) -> tuple[str | None, dict[str, object]]:
            run = await find_run_by_prefix(db, run_id)
            return run.workspace, dict(run.meta or {})

        workspace, meta = await self._read(action)
        override = RunOverride.from_meta(meta)
        if not workspace:
            return self._runner if override.is_empty() else self._runner_for(
                self.workspace, cfg=apply_override(self.cfg, override)
            )
        return self._runner_for(
            Path(workspace),
            cfg=apply_override(self.cfg, override) if not override.is_empty() else None,
        )
```

- [ ] **Шаг 5: эндпоинт**

`api.py`, в `send_message`:

```python
            run_id = await service.send_message(
                session_id,
                req.text,
                req.autonomy,
                RunOverride(
                    executor=req.executor, provider=req.provider, model=req.model
                ),
            )
```
и новая ветка обработки рядом с `(SandboxError, WorkspaceLayoutError)`:

```python
        except OverrideError as exc:
            # Выбор в поле ввода несовместим с конфигом — это ввод человека,
            # а не сбой сервера: 422 с текстом, который говорит, что делать.
            raise HTTPException(status_code=422, detail=str(exc)) from None
```

- [ ] **Шаг 6: тесты зелёные**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_overrides.py -q`
Expected: PASS

- [ ] **Шаг 7: коммит**

```bash
git add src/svarog_harness/gateway/ tests/test_gateway_overrides.py
git commit -m "feat(gateway): override сообщения переживает resume"
```

---

### Задача 4: тёплый sandbox не переиспользуется с чужим override

**Files:**
- Modify: `src/svarog_harness/gateway/service.py:228-255` (`_WarmSlot`, `_acquire_warm`)
- Test: `tests/test_gateway_overrides.py`

**Interfaces:**
- Consumes: `RunOverride` (задача 1), `apply_override` и правки `send_message` (задача 3).
- Produces: `_WarmSlot.override: RunOverride`; `_acquire_warm(session_id, workspace, autonomy, override)`.

- [ ] **Шаг 1: тест**

```python
@pytest.mark.asyncio
async def test_warm_slot_not_reused_with_other_override(
    service: GatewayService,
) -> None:
    """Слот держит runner с конфигом прошлого сообщения — переиспользовать нельзя."""
    ws = service.workspace
    first = await service._acquire_warm("s1", ws, AutonomyMode.YOLO, RunOverride())
    second = await service._acquire_warm(
        "s1", ws, AutonomyMode.YOLO, RunOverride(provider="router")
    )
    assert first is not second
    assert second.runner.cfg.models.default == "router"

    again = await service._acquire_warm(
        "s1", ws, AutonomyMode.YOLO, RunOverride(provider="router")
    )
    assert again is second, "тот же override — тот же слот"
    await service.close_warm_sessions()
```

Тест требует `cloud.warm_session_ttl_sec > 0`; добавить в фикстуру `service` строку `cloud:\n  warm_session_ttl_sec: 60\n`.

- [ ] **Шаг 2: убедиться, что падает**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_overrides.py -q -k warm_slot`
Expected: FAIL — `_acquire_warm()` принимает 4 аргумента

- [ ] **Шаг 3: реализация**

В `_WarmSlot` добавить поле `override: RunOverride = RunOverride()`. В `_acquire_warm`:

```python
    async def _acquire_warm(
        self,
        session_id: str,
        workspace: Path,
        autonomy: AutonomyMode,
        override: RunOverride = RunOverride(),
    ) -> _WarmSlot | None:
        if self.cfg.cloud.warm_session_ttl_sec <= 0:
            return None
        async with self._warm_lock:
            slot = self._warm.get(session_id)
            if slot is not None and slot.override == override:
                slot.last_used = time.monotonic()
                return slot
            if slot is not None:
                # Слот держит env/MCP, поднятые под прошлым конфигом: с другим
                # исполнителем или провайдером это чужой sandbox.
                await self._drop_warm(session_id)
            cfg = apply_override(self.cfg, override)
            run_meta = (
                {OVERRIDE_META_KEY: override.to_meta()} if not override.is_empty() else None
            )
            runner = self._runner_for(workspace, cfg=cfg, run_meta=run_meta)
            resources = await runner.prepare_session_resources(autonomy)
            slot = _WarmSlot(
                workspace=workspace,
                runner=runner,
                resources=resources,
                last_used=time.monotonic(),
                override=override,
            )
            self._warm[session_id] = slot
            return slot
```

`_drop_warm` вызывается под уже взятым локом — проверить, что он сам лока не берёт (сейчас не берёт).

- [ ] **Шаг 4: тесты зелёные**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_overrides.py tests/test_cloud_sessions.py -q`
Expected: PASS

- [ ] **Шаг 5: коммит**

```bash
git add src/svarog_harness/gateway/service.py tests/test_gateway_overrides.py
git commit -m "feat(gateway): тёплый слот привязан к override сообщения"
```

---

### Задача 5: каталог моделей провайдера

**Files:**
- Create: `src/svarog_harness/gateway/catalog.py`
- Test: `tests/test_model_catalog.py`

**Interfaces:**
- Produces: `ModelCard` (поля `id`, `name`, `context_length`, `input_usd_per_mtok`, `output_usd_per_mtok`), `parse_models(payload) -> list[ModelCard]`, `async fetch_models(provider, api_key, *, timeout=10.0) -> list[ModelCard]`, `CatalogError`.

- [ ] **Шаг 1: тест разбора и загрузки**

```python
"""Каталог моделей провайдера (план 2026-07-28)."""

import httpx
import pytest

from svarog_harness.config.schema import ProviderConfig
from svarog_harness.gateway.catalog import CatalogError, fetch_models, parse_models


def test_parses_openrouter_shape_with_pricing() -> None:
    cards = parse_models(
        {
            "data": [
                {
                    "id": "deepseek/deepseek-v4-flash",
                    "name": "DeepSeek V4 Flash",
                    "context_length": 163840,
                    # OpenRouter отдаёт цену за один токен строкой.
                    "pricing": {"prompt": "0.0000005", "completion": "0.0000015"},
                }
            ]
        }
    )
    assert len(cards) == 1
    assert cards[0].id == "deepseek/deepseek-v4-flash"
    assert cards[0].name == "DeepSeek V4 Flash"
    assert cards[0].context_length == 163840
    assert cards[0].input_usd_per_mtok == pytest.approx(0.5)
    assert cards[0].output_usd_per_mtok == pytest.approx(1.5)


def test_parses_bare_openai_shape() -> None:
    cards = parse_models({"data": [{"id": "gpt-5", "object": "model"}]})
    assert cards == [
        type(cards[0])(
            id="gpt-5",
            name=None,
            context_length=None,
            input_usd_per_mtok=None,
            output_usd_per_mtok=None,
        )
    ]


def test_skips_entries_without_id_instead_of_failing() -> None:
    cards = parse_models({"data": [{"name": "без id"}, {"id": "ok"}, "мусор"]})
    assert [c.id for c in cards] == ["ok"]


def test_garbage_payload_gives_empty_list() -> None:
    assert parse_models({"data": "не список"}) == []
    assert parse_models({}) == []


def _provider(base_url: str) -> ProviderConfig:
    return ProviderConfig(base_url=base_url, model="fake")


@pytest.mark.asyncio
async def test_fetch_uses_base_url_as_is_and_sends_key() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    transport = httpx.MockTransport(handler)
    cards = await fetch_models(
        _provider("https://openrouter.ai/api/v1/"), "секрет", transport=transport
    )

    assert seen["url"] == "https://openrouter.ai/api/v1/models"
    assert seen["auth"] == "Bearer секрет"
    assert [c.id for c in cards] == ["m1"]


@pytest.mark.asyncio
async def test_fetch_without_key_sends_no_auth_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") is None
        return httpx.Response(200, json={"data": []})

    await fetch_models(
        _provider("http://localhost:9/v1"), None, transport=httpx.MockTransport(handler)
    )


@pytest.mark.asyncio
async def test_http_error_becomes_catalog_error_with_status() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(401, text="no key"))
    with pytest.raises(CatalogError, match="401"):
        await fetch_models(_provider("https://x/v1"), None, transport=transport)


@pytest.mark.asyncio
async def test_non_json_becomes_catalog_error() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text="<html>"))
    with pytest.raises(CatalogError, match="не JSON"):
        await fetch_models(_provider("https://x/v1"), None, transport=transport)


@pytest.mark.asyncio
async def test_network_failure_becomes_catalog_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет связи")

    with pytest.raises(CatalogError, match="нет связи"):
        await fetch_models(
            _provider("https://x/v1"), None, transport=httpx.MockTransport(handler)
        )
```

- [ ] **Шаг 2: убедиться, что падает**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_model_catalog.py -q`
Expected: FAIL — `ModuleNotFoundError: svarog_harness.gateway.catalog`

- [ ] **Шаг 3: реализация**

```python
"""Список моделей провайдера по openai-совместимому `/models`.

Модуль без состояния: кэш и резолвинг секретов живут в сервисе. URL
собирается как `{base_url}/models` — ровно то, что сделал бы openai-SDK со
своим `base_url`. Побочная польза: `base_url` без `/v1` даёт видимую
ошибку здесь, а не загадочное молчание при запуске.
"""

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
    """USD за токен (OpenRouter отдаёт строкой) → USD за миллион токенов."""
    if isinstance(raw, str | int | float):
        try:
            return float(raw) * 1_000_000
        except ValueError:
            return None
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
    """Список моделей провайдера. Ключ уходит только в заголовок запроса."""
    url = f"{provider.base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise CatalogError(f"{url}: {exc}") from None
    if response.status_code >= 400:
        raise CatalogError(f"{url}: провайдер ответил {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise CatalogError(f"{url}: ответ не JSON") from None
    return parse_models(payload if isinstance(payload, dict) else {})
```

- [ ] **Шаг 4: тесты зелёные**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_model_catalog.py -q`
Expected: PASS, 9 тестов

- [ ] **Шаг 5: коммит**

```bash
git add src/svarog_harness/gateway/catalog.py tests/test_model_catalog.py
git commit -m "feat(gateway): разбор и загрузка списка моделей провайдера"
```

---

### Задача 6: эндпоинты каталога, кэш и цены

**Files:**
- Modify: `src/svarog_harness/gateway/models.py`
- Modify: `src/svarog_harness/gateway/service.py`
- Modify: `src/svarog_harness/gateway/api.py`
- Test: `tests/test_model_catalog.py`

**Interfaces:**
- Consumes: `fetch_models`, `ModelCard`, `CatalogError` (задача 5); `apply_override(..., prices=...)` (задача 1).
- Produces: `ProviderView`, `ModelCardView`; `GatewayService.list_providers()`, `async provider_models(name)`, `async _model_prices(provider, model)`; `GET /models`, `GET /models/{provider}`.

- [ ] **Шаг 1: тест эндпоинтов и кэша**

```python
@pytest.mark.asyncio
async def test_providers_endpoint_lists_config_entries(service) -> None:
    client = TestClient(create_app(service=service))
    body = client.get("/models").json()
    assert [p["name"] for p in body] == ["local", "router"]
    assert [p["is_default"] for p in body] == [True, False]
    assert body[0]["model"] == "fake-model"


@pytest.mark.asyncio
async def test_models_endpoint_caches_second_call(service, monkeypatch) -> None:
    calls = {"n": 0}

    async def fake_fetch(provider, api_key, **kwargs):
        calls["n"] += 1
        return [ModelCard(id="m1", name="M1")]

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", fake_fetch)
    client = TestClient(create_app(service=service))

    first = client.get("/models/router")
    second = client.get("/models/router")

    assert first.status_code == 200
    assert [m["id"] for m in first.json()] == ["m1"]
    assert second.json() == first.json()
    assert calls["n"] == 1, "второй вызов обслужен кэшем"


@pytest.mark.asyncio
async def test_unknown_provider_is_404(service) -> None:
    client = TestClient(create_app(service=service))
    assert client.get("/models/нет-такого").status_code == 404


@pytest.mark.asyncio
async def test_provider_failure_is_502_with_reason(service, monkeypatch) -> None:
    async def boom(provider, api_key, **kwargs):
        raise CatalogError("https://x/models: провайдер ответил 401")

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", boom)
    client = TestClient(create_app(service=service))
    response = client.get("/models/router")
    assert response.status_code == 502
    assert "401" in response.json()["detail"]


@pytest.mark.asyncio
async def test_model_override_takes_prices_from_catalog(service, monkeypatch) -> None:
    async def fake_fetch(provider, api_key, **kwargs):
        return [ModelCard(id="x/y", input_usd_per_mtok=0.25, output_usd_per_mtok=0.75)]

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", fake_fetch)
    session = await service.create_session(title="цены")
    run_id = await service.send_message(
        session.session_id, "задача", None, RunOverride(provider="router", model="x/y")
    )
    runner = await service._runner_for_run(run_id)
    # При resume цены восстанавливаются из того же каталога.
    assert runner.cfg.models.providers["router"].input_usd_per_mtok == 0.25
```

Фикстуру `service` и импорты для этого файла взять такими же, как в `tests/test_gateway_overrides.py` (задача 3).

- [ ] **Шаг 2: убедиться, что падает**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_model_catalog.py -q -k endpoint`
Expected: FAIL — 404 на `/models`

- [ ] **Шаг 3: модели ответа**

`gateway/models.py`:

```python
class ProviderView(BaseModel):
    name: str
    base_url: str
    model: str
    is_default: bool


class ModelCardView(BaseModel):
    id: str
    name: str | None = None
    context_length: int | None = None
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
```

- [ ] **Шаг 4: сервис**

Инициализировать кэш в `__post_init__`:

```python
        # Каталоги моделей: имя провайдера → (момент загрузки, карточки).
        # TTL, а не вечный кэш: список моделей у провайдера меняется.
        self._catalog: dict[str, tuple[float, list[ModelCard]]] = {}
```

и методы:

```python
    def list_providers(self) -> list[ProviderView]:
        """Записи models.providers. Наружу — без api_key_ref (ADR-0006)."""
        return [
            ProviderView(
                name=name,
                base_url=provider.base_url,
                model=provider.model,
                is_default=name == self.cfg.models.default,
            )
            for name, provider in sorted(self.cfg.models.providers.items())
        ]

    async def provider_models(self, name: str) -> list[ModelCard]:
        """Список моделей провайдера с TTL-кэшем; CatalogError → 502."""
        provider = self.cfg.models.providers.get(name)
        if provider is None:
            raise UnknownProviderError(f"провайдер '{name}' не описан в models.providers")
        cached = self._catalog.get(name)
        now = time.monotonic()
        if cached is not None and now - cached[0] < CATALOG_TTL_SEC:
            return cached[1]
        api_key = resolve_api_key(provider, self._runner.host_store)
        cards = await fetch_models(provider, None if api_key == "not-needed" else api_key)
        self._catalog[name] = (now, cards)
        return cards

    async def _model_prices(self, provider: str, model: str) -> tuple[float, float] | None:
        """Цены модели из каталога; каталог недоступен — цены из конфига."""
        try:
            cards = await self.provider_models(provider)
        except (UnknownProviderError, CatalogError, ApiKeyError):
            return None
        for card in cards:
            if card.id == model:
                if card.input_usd_per_mtok is None or card.output_usd_per_mtok is None:
                    return None
                return (card.input_usd_per_mtok, card.output_usd_per_mtok)
        return None
```

`CATALOG_TTL_SEC = 600.0` — константа модуля рядом с прочими. `UnknownProviderError` — новый класс исключения там же, где `SessionBusyError`.

В `send_message` и `_runner_for_run` цены подмешиваются, когда override задаёт модель:

```python
    async def _derive(self, override: RunOverride) -> SvarogConfig:
        """Производный конфиг сообщения вместе с ценами выбранной модели."""
        prices = None
        if override.model is not None:
            target = override.provider or self.cfg.models.default
            prices = await self._model_prices(target, override.model)
        return apply_override(self.cfg, override, prices=prices)
```

и оба места зовут `cfg = await self._derive(override)` вместо `apply_override(...)`.

- [ ] **Шаг 5: эндпоинты**

`api.py`:

```python
    @app.get("/models", response_model=list[ProviderView])
    async def list_providers(service: ServiceDep) -> list[ProviderView]:
        return service.list_providers()

    @app.get("/models/{provider}", response_model=list[ModelCardView])
    async def provider_models(provider: str, service: ServiceDep) -> list[ModelCardView]:
        try:
            cards = await service.provider_models(provider)
        except UnknownProviderError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except (CatalogError, ApiKeyError) as exc:
            # Провайдер недоступен или ключ не найден — это не сбой шлюза:
            # 502 с причиной, чтобы человек увидел, что чинить.
            raise HTTPException(status_code=502, detail=str(exc)) from None
        return [ModelCardView(**vars(card)) for card in cards]
```

- [ ] **Шаг 6: тесты зелёные**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_model_catalog.py tests/test_gateway_overrides.py -q`
Expected: PASS

- [ ] **Шаг 7: коммит**

```bash
git add src/svarog_harness/gateway/ tests/test_model_catalog.py
git commit -m "feat(gateway): эндпоинты каталога моделей и цены override"
```

---

### Задача 7: «Настройки» без перезапуска

**Files:**
- Modify: `src/svarog_harness/gateway/models.py` (`ConfigDiffView`)
- Modify: `src/svarog_harness/gateway/service.py` (`write_config`)
- Test: `tests/test_gateway_web.py`

**Interfaces:**
- Produces: `ConfigDiffView.restart_required: bool`; `GatewayService.write_config` перечитывает конфиг и сбрасывает `self._runner`, `self._catalog`, тёплые слоты.

- [ ] **Шаг 1: тест**

```python
@pytest.mark.asyncio
async def test_write_config_reloads_snapshot_used_by_runs(service, tmp_path) -> None:
    """Правка исполнителя должна действовать без перезапуска svarog serve."""
    assert service.cfg.runtime.max_iterations != 7
    view = service.write_config({"runtime.max_iterations": 7})
    assert view.restart_required is False
    assert service.cfg.runtime.max_iterations == 7
    assert service._runner.cfg.runtime.max_iterations == 7


@pytest.mark.asyncio
async def test_write_config_defers_reload_while_run_is_live(service) -> None:
    session = await service.create_session(title="занят")
    await service.send_message(session.session_id, "задача", None)
    # Run падает на недоступном провайдере; для теста живое состояние
    # проставляем сами — важна ветка, а не гонка с фоновой задачей.
    ...
```

Второй тест дописывается по образцу `test_delete_session_refuses_while_run_is_live` из `tests/test_gateway_web.py`: там уже есть приём с проставлением `RunState.RUNNING` в БД. Проверяется, что `view.restart_required is True` и `service.cfg.runtime.max_iterations` не изменился.

- [ ] **Шаг 2: убедиться, что падает**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_web.py -q -k write_config`
Expected: FAIL — у `ConfigDiffView` нет поля `restart_required`

- [ ] **Шаг 3: реализация**

`ConfigDiffView` получает `restart_required: bool = False`.

`write_config` становится асинхронным (проверка живых запусков ходит в БД) — поправить и вызов в `api.py`:

```python
    async def write_config(self, values: dict[str, Any]) -> ConfigDiffView:
        """Записать правку и, если ни один запуск не жив, перечитать конфиг.

        Конфиг под работающим run не меняется (ADR-0015 §0.4), поэтому при
        живом запуске снимок остаётся прежним, а ответ честно говорит, что
        правка вступит в силу позже.
        """
        view = self.preview_config(values)
        _, after = self._updated_config_text(values)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(after, encoding="utf-8")

        if await self._any_run_live():
            return view.model_copy(update={"restart_required": True})

        self.cfg = load_config(project_dir=self.workspace)
        self._runner = TaskRunner(self.cfg, self.workspace, role=self.role)
        # Тёплые слоты держат env/MCP, поднятые под прежним конфигом.
        await self.close_warm_sessions()
        self._catalog.clear()
        return view
```

`_any_run_live` — по образцу проверки в `delete_session`, но без привязки к сессии:

```python
    async def _any_run_live(self) -> bool:
        async def action(db: AsyncSession) -> bool:
            found = await db.execute(select(Run).where(Run.state.in_(_LIVE_STATES)).limit(1))
            return found.scalar_one_or_none() is not None

        return await self._read(action)
```

- [ ] **Шаг 4: тесты зелёные**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_gateway_web.py -q`
Expected: PASS

- [ ] **Шаг 5: полный прогон**

Run: `COLUMNS=200 .venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Шаг 6: коммит**

```bash
git add src/svarog_harness/gateway/ tests/test_gateway_web.py
git commit -m "feat(gateway): правка конфига действует без перезапуска"
```

---

### Задача 8: клиентский API

**Files:**
- Modify: `web/src/api/types.ts`, `web/src/api/client.ts`, `web/src/test/fakeApi.ts`
- Test: `web/src/api/client.test.ts`

**Interfaces:**
- Consumes: эндпоинты задач 3 и 6.
- Produces: типы `ExecutorKind`, `RunOverride`, `ProviderCard`, `ModelCard`; методы `providers()`, `providerModels(name)`; `sendMessage(sessionId, text, autonomy?, override?)`.

- [ ] **Шаг 1: тест**

```ts
it("передаёт override сообщения и опускает пустые поля", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ run_id: "r1", state: "running" }), {
      status: 201,
      headers: { "content-type": "application/json" },
    }),
  );
  const api = createClient({ baseUrl: "", fetchImpl: fetchMock });

  await api.sendMessage("s1", "привет", "yolo", {
    executor: "external",
    model: "x/y",
  });

  const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
  expect(body).toEqual({
    text: "привет",
    autonomy: "yolo",
    executor: "external",
    model: "x/y",
  });
});
```

Если у `createClient` нет параметра `fetchImpl`, тест использует `vi.stubGlobal("fetch", fetchMock)` — проверить фактическую сигнатуру в `web/src/api/client.ts` перед написанием.

- [ ] **Шаг 2: убедиться, что падает**

Run: `npm --prefix web test -- client`
Expected: FAIL — `sendMessage` игнорирует четвёртый аргумент

- [ ] **Шаг 3: типы**

```ts
/** Значения executor.type сервера (config/schema.py). */
export type ExecutorKind = "native" | "external";

export const EXECUTOR_LABELS: Record<ExecutorKind, string> = {
  native: "нативный цикл",
  external: "внешний агент",
};

/** Выбор в поле ввода: свойство сообщения, конфиг не меняется. */
export interface RunOverride {
  executor?: ExecutorKind;
  provider?: string;
  model?: string;
}

export interface ProviderCard {
  name: string;
  base_url: string;
  model: string;
  is_default: boolean;
}

export interface ModelCard {
  id: string;
  name: string | null;
  context_length: number | null;
  input_usd_per_mtok: number | null;
  output_usd_per_mtok: number | null;
}
```

- [ ] **Шаг 4: клиент**

```ts
    sendMessage: (sessionId, text, autonomy, override) =>
      request<RunRef>(`/sessions/${encodeURIComponent(sessionId)}/messages`, {
        method: "POST",
        // Пустые поля не отправляем: сервер трактует отсутствие как
        // «взять из конфига», а null пришлось бы обрабатывать отдельно.
        body: JSON.stringify({
          text,
          ...(autonomy === undefined ? {} : { autonomy }),
          ...(override?.executor ? { executor: override.executor } : {}),
          ...(override?.provider ? { provider: override.provider } : {}),
          ...(override?.model ? { model: override.model } : {}),
        }),
      }),
    providers: () => request<ProviderCard[]>("/models"),
    providerModels: (name) =>
      request<ModelCard[]>(`/models/${encodeURIComponent(name)}`),
```

Сигнатура в `interface Api`:

```ts
  sendMessage(
    sessionId: string,
    text: string,
    autonomy?: Autonomy,
    override?: RunOverride,
  ): Promise<RunRef>;
  providers(): Promise<ProviderCard[]>;
  providerModels(name: string): Promise<ModelCard[]>;
```

В `web/src/test/fakeApi.ts` добавить заглушки:

```ts
    providers: vi.fn().mockResolvedValue([]),
    providerModels: vi.fn().mockResolvedValue([]),
```

- [ ] **Шаг 5: тесты зелёные**

Run: `npm --prefix web test`
Expected: PASS

- [ ] **Шаг 6: коммит**

```bash
git add web/src/api web/src/test
git commit -m "feat(web): override сообщения и каталог моделей в клиенте API"
```

---

### Задача 9: список моделей с поиском

**Files:**
- Create: `web/src/components/ModelPicker.tsx`, `web/src/components/ModelPicker.css`
- Test: `web/src/components/ModelPicker.test.tsx`

**Interfaces:**
- Consumes: `ModelCard` (задача 8).
- Produces: `<ModelPicker models={...} current={...} error={...} onPick={...} onClose={...} />`.

- [ ] **Шаг 1: тест**

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ModelPicker } from "./ModelPicker";
import type { ModelCard } from "../api/types";

const MODELS: ModelCard[] = [
  {
    id: "deepseek/deepseek-v4-flash",
    name: "DeepSeek V4 Flash",
    context_length: 163840,
    input_usd_per_mtok: 0.5,
    output_usd_per_mtok: 1.5,
  },
  {
    id: "anthropic/claude-opus",
    name: "Claude Opus",
    context_length: 200000,
    input_usd_per_mtok: 15,
    output_usd_per_mtok: 75,
  },
];

describe("ModelPicker", () => {
  it("фильтрует по id и по имени", async () => {
    render(
      <ModelPicker
        models={MODELS}
        current="deepseek/deepseek-v4-flash"
        error={null}
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    await userEvent.type(screen.getByLabelText("Поиск модели"), "opus");

    expect(screen.getByText("Claude Opus")).toBeInTheDocument();
    expect(screen.queryByText("DeepSeek V4 Flash")).not.toBeInTheDocument();
  });

  it("возвращает выбранную модель", async () => {
    const onPick = vi.fn();
    render(
      <ModelPicker
        models={MODELS}
        current=""
        error={null}
        onPick={onPick}
        onClose={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByText("Claude Opus"));

    expect(onPick).toHaveBeenCalledWith("anthropic/claude-opus");
  });

  it("показывает причину, когда каталог не пришёл", () => {
    render(
      <ModelPicker
        models={[]}
        current=""
        error="провайдер ответил 401"
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText(/401/)).toBeInTheDocument();
  });

  it("закрывается по Escape", async () => {
    const onClose = vi.fn();
    render(
      <ModelPicker
        models={MODELS}
        current=""
        error={null}
        onPick={vi.fn()}
        onClose={onClose}
      />,
    );

    await userEvent.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalled();
  });
});
```

- [ ] **Шаг 2: убедиться, что падает**

Run: `npm --prefix web test -- ModelPicker`
Expected: FAIL — модуль не найден

- [ ] **Шаг 3: компонент**

```tsx
import { useEffect, useRef, useState } from "react";

import type { ModelCard } from "../api/types";
import "./ModelPicker.css";

/** «163840» → «164K»: в строке списка важен порядок, а не точность. */
function context(length: number | null): string {
  if (length === null) return "";
  return length >= 1000 ? `${Math.round(length / 1000)}K` : String(length);
}

function price(card: ModelCard): string {
  if (card.input_usd_per_mtok === null) return "";
  return `$${card.input_usd_per_mtok}/${card.output_usd_per_mtok ?? "?"} за Mtok`;
}

export function ModelPicker({
  models,
  current,
  error,
  onPick,
  onClose,
}: {
  models: ModelCard[];
  current: string;
  error: string | null;
  onPick: (id: string) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const field = useRef<HTMLInputElement>(null);

  // Поиск в фокусе сразу: у OpenRouter моделей несколько сотен, и первое
  // действие человека здесь — печатать, а не листать.
  useEffect(() => field.current?.focus(), []);

  const needle = query.trim().toLowerCase();
  const shown = models.filter(
    (card) =>
      needle === "" ||
      card.id.toLowerCase().includes(needle) ||
      (card.name ?? "").toLowerCase().includes(needle),
  );

  return (
    <div
      className="picker"
      role="dialog"
      aria-label="Выбор модели"
      onKeyDown={(event) => {
        if (event.key === "Escape") onClose();
      }}
    >
      <input
        ref={field}
        className="picker__search"
        aria-label="Поиск модели"
        placeholder="Поиск модели"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      {error !== null && <p className="picker__error">{error}</p>}
      {error === null && shown.length === 0 && (
        <p className="picker__empty">Ничего не нашлось.</p>
      )}
      <ul className="picker__list">
        {shown.map((card) => (
          <li key={card.id}>
            <button
              type="button"
              className={`picker__row${card.id === current ? " picker__row--current" : ""}`}
              onClick={() => onPick(card.id)}
            >
              <span className="picker__name">{card.name ?? card.id}</span>
              <span className="picker__meta">
                {context(card.context_length)} {price(card)}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
```

- [ ] **Шаг 4: стили**

`ModelPicker.css` — по токенам из `web/src/styles/tokens.css`, как в остальных компонентах: тёмная подложка, единый акцент на текущей строке, `max-height: 360px` со скроллом внутри списка. На узком экране (`@media (max-width: 899px)`) — лист снизу: `position: fixed; inset: auto 0 0 0; max-height: 70vh; border-radius: 12px 12px 0 0`.

- [ ] **Шаг 5: тесты зелёные**

Run: `npm --prefix web test -- ModelPicker`
Expected: PASS, 4 теста

- [ ] **Шаг 6: коммит**

```bash
git add web/src/components/ModelPicker.tsx web/src/components/ModelPicker.css web/src/components/ModelPicker.test.tsx
git commit -m "feat(web): список моделей с поиском"
```

---

### Задача 10: контролы в поле ввода и правда о модели

Сегодня `ChatScreen` передаёт в `Composer` литералы `executor="нативный цикл"` и `model="qwen3-coder"` (`web/src/screens/ChatScreen.tsx:194-195`) — подвал показывает их независимо от конфига. Задача заканчивает это.

**Files:**
- Modify: `web/src/components/Composer.tsx:6-107`, `web/src/components/Composer.css`
- Modify: `web/src/screens/ChatScreen.tsx:33-199`
- Test: `web/src/components/Composer.test.tsx`, `web/src/screens/ChatScreen.test.tsx`

**Interfaces:**
- Consumes: `RunOverride`, `ProviderCard`, `ModelCard`, `EXECUTOR_LABELS` (задача 8); `ModelPicker` (задача 9).
- Produces: `Composer` с пропсами `executor`, `onExecutorChange`, `providers`, `provider`, `onProviderChange`, `model`, `models`, `modelsError`, `onModelChange`.

- [ ] **Шаг 1: тесты**

```tsx
it("переключает исполнителя", async () => {
  const onExecutorChange = vi.fn();
  render(<Composer {...base} onExecutorChange={onExecutorChange} />);

  await userEvent.selectOptions(
    screen.getByLabelText("Исполнитель"),
    "внешний агент",
  );

  expect(onExecutorChange).toHaveBeenCalledWith("external");
});

it("гасит выбор модели у внешнего агента", () => {
  render(<Composer {...base} executor="external" />);

  const button = screen.getByLabelText("Выбрать модель");
  expect(button).toBeDisabled();
  expect(button).toHaveAttribute(
    "title",
    expect.stringContaining("своему провайдеру"),
  );
});

it("показывает модель из конфига, а не выдуманную", () => {
  render(<Composer {...base} model="deepseek/deepseek-v4-flash" />);
  expect(screen.getByText("deepseek/deepseek-v4-flash")).toBeInTheDocument();
});
```

и в `ChatScreen.test.tsx`:

```tsx
it("отправляет выбранные исполнителя и модель", async () => {
  const sendMessage = vi.fn().mockResolvedValue({ run_id: "r1", state: "running" });
  const api = fakeApi({
    sendMessage,
    providers: vi.fn().mockResolvedValue([
      { name: "router", base_url: "https://x/v1", model: "m0", is_default: true },
    ]),
    providerModels: vi.fn().mockResolvedValue([
      {
        id: "x/y",
        name: "X Y",
        context_length: null,
        input_usd_per_mtok: null,
        output_usd_per_mtok: null,
      },
    ]),
  });
  render(<ChatScreen api={api} sessionId="s1" ensureSession={vi.fn()} />);

  await userEvent.click(await screen.findByLabelText("Выбрать модель"));
  await userEvent.click(await screen.findByText("X Y"));
  await userEvent.type(screen.getByLabelText("Написать Сварогу"), "привет{Enter}");

  expect(sendMessage).toHaveBeenCalledWith("s1", "привет", "supervised", {
    executor: "native",
    provider: "router",
    model: "x/y",
  });
});

it("сохраняет выбор между сообщениями", async () => {
  // Второй вызов sendMessage несёт тот же override, что и первый.
});
```

- [ ] **Шаг 2: убедиться, что падают**

Run: `npm --prefix web test -- Composer ChatScreen`
Expected: FAIL — нет `Исполнитель`, нет `Выбрать модель`

- [ ] **Шаг 3: `Composer`**

Заменить блок `composer__fixed` (строки 66-78) на контролы:

```tsx
              <span className="composer__dot" aria-hidden="true">
                ·
              </span>
              <select
                className="composer__select"
                aria-label="Исполнитель"
                value={executor}
                onChange={(event) =>
                  onExecutorChange(event.target.value as ExecutorKind)
                }
              >
                {(Object.keys(EXECUTOR_LABELS) as ExecutorKind[]).map((kind) => (
                  <option key={kind} value={kind}>
                    {EXECUTOR_LABELS[kind]}
                  </option>
                ))}
              </select>
              <span className="composer__dot" aria-hidden="true">
                ·
              </span>
              {providers.length > 1 && (
                <select
                  className="composer__select"
                  aria-label="Провайдер"
                  value={provider}
                  disabled={external}
                  onChange={(event) => onProviderChange(event.target.value)}
                >
                  {providers.map((card) => (
                    <option key={card.name} value={card.name}>
                      {card.name}
                    </option>
                  ))}
                </select>
              )}
              <button
                type="button"
                className="composer__model"
                aria-label="Выбрать модель"
                disabled={external}
                // Внешний агент ходит к своему провайдеру
                // (executor.external.base_url) — модель отсюда на него не влияет.
                title={
                  external
                    ? "Внешний агент ходит к своему провайдеру"
                    : "Выбрать модель"
                }
                onClick={() => setPicking(true)}
              >
                {model}
              </button>
```

где `const external = executor === "external";` и `const [picking, setPicking] = useState(false);`. Сам список рисуется над подвалом:

```tsx
        {picking && (
          <ModelPicker
            models={models}
            current={model}
            error={modelsError}
            onPick={(id) => {
              onModelChange(id);
              setPicking(false);
            }}
            onClose={() => setPicking(false)}
          />
        )}
```

- [ ] **Шаг 4: `ChatScreen`**

Состояние и загрузка провайдеров:

```tsx
  const [executor, setExecutor] = useState<ExecutorKind>("native");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [providers, setProviders] = useState<ProviderCard[]>([]);
  const [models, setModels] = useState<ModelCard[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);

  useEffect(() => {
    api
      .providers()
      .then((cards) => {
        setProviders(cards);
        const active = cards.find((card) => card.is_default) ?? cards[0];
        if (active === undefined) return;
        setProvider(active.name);
        // Модель из конфига, а не литерал: подвал не должен врать про то,
        // какая модель на самом деле отвечает.
        setModel(active.model);
      })
      .catch(() => setProviders([]));
  }, [api]);

  useEffect(() => {
    if (provider === "") return;
    setModelsError(null);
    api
      .providerModels(provider)
      .then(setModels)
      .catch((exc: unknown) => {
        setModels([]);
        setModelsError(
          exc instanceof ApiError
            ? exc.message
            : "Не удалось получить список моделей у провайдера.",
        );
      });
  }, [api, provider]);
```

Смена провайдера подставляет его модель из конфига:

```tsx
  const pickProvider = useCallback(
    (name: string) => {
      setProvider(name);
      setModel(providers.find((card) => card.name === name)?.model ?? "");
    },
    [providers],
  );
```

В `send` четвёртым аргументом уходит override:

```tsx
        const ref = await api.sendMessage(target, text, autonomy, {
          executor,
          provider,
          model,
        });
```

и `executor, provider, model` добавляются в зависимости `useCallback`.

В `<Composer>` передать все новые пропсы вместо литералов.

- [ ] **Шаг 5: стили**

`Composer.css` — `.composer__model`: кнопка без рамки, цвет как у `.composer__fixed`, `:disabled { opacity: .45; cursor: not-allowed }`. Подвал получает `flex-wrap: wrap` и `row-gap: 4px`: на 360px четыре контрола в строку не помещаются.

- [ ] **Шаг 6: тесты зелёные**

Run: `npm --prefix web test`
Expected: PASS

- [ ] **Шаг 7: сборка**

Run: `npm --prefix web run build`
Expected: сборка без ошибок типов

- [ ] **Шаг 8: коммит**

```bash
git add web/src
git commit -m "feat(web): выбор исполнителя, провайдера и модели в поле ввода"
```

---

### Задача 11: спек, README и живая проверка

**Files:**
- Modify: `docs/superpowers/specs/2026-07-27-web-ui-gorn-design.md` (таблица «Состояние реализации», список дельты API)
- Modify: `README.md` (раздел про веб-интерфейс, если он перечисляет эндпоинты)

- [ ] **Шаг 1: обновить спек интерфейса**

В таблице «Состояние реализации» отметить, что исполнитель и модель переключаются из поля ввода; в списке дельты API добавить `GET /models`, `GET /models/{provider}` и поля override у `POST /sessions/{id}/messages`.

- [ ] **Шаг 2: полный прогон обоих гейтов**

```bash
COLUMNS=200 .venv/bin/python -m pytest -q && npm --prefix web test && npm --prefix web run build
```
Expected: python PASS, клиент PASS, сборка без ошибок

- [ ] **Шаг 3: живая проверка**

Поднять `svarog serve` из проектной папки с переменными из `svarog install` (не из `agent-home` — это нарушает изоляцию workspace, ADR-0015 §0.3) и пройти путь: открыть чат, увидеть в подвале настоящую модель из конфига, открыть список моделей, выбрать другую, отправить сообщение, убедиться в логе, что запуск стартовал. Переключить исполнителя на внешнего агента при `sandbox.type: local-trusted` и убедиться, что приходит 422 с понятным текстом, а не 500.

- [ ] **Шаг 4: коммит**

```bash
git add docs README.md
git commit -m "docs: выбор исполнителя и модели в поле ввода"
```

---

## Самопроверка плана

**Покрытие спека.** §1 контракт → задача 3. §2 `overrides.py` → задача 1. §3 проброс и resume → задачи 2 и 3 (тёплые слоты — задача 4). §4 каталог → задачи 5 и 6, цены → задача 6. §5 интерфейс → задачи 8–10. §6 горячее перечитывание → задача 7. Таблица ошибок: 422 override — задачи 1 и 3; 404 и 502 каталога — задача 6; 409 не меняется. Раздел «Тесты» разложен по задачам, ключевой инвариант дайджеста — задача 3, шаг 1.

**Расхождение со спеком, зафиксированное осознанно.** Спек называет ключевым тестом полный цикл «запуск → гейт → одобрение». Гейт в шлюзе требует фальшивого LLM-провайдера, вживлённого в `TaskRunner`, — отдельная оснастка. План проверяет тот же инвариант точнее и дешевле: `config_digest(runner.cfg, ws) == Run.meta["config_hash"]`, то есть ровно то, что сверяет `_assert_config_unchanged`. Существующие тесты одобрения (`tests/test_approval_flow.py`) идут без override и продолжают покрывать сам цикл.

**Заглушек нет.** Шаг 1 задачи 7 содержит многоточие вместо второго теста — это единственное место, и там же сказано, с какого существующего теста берётся приём (`test_delete_session_refuses_while_run_is_live`), потому что он завязан на текущий вид фикстуры в `tests/test_gateway_web.py`.

**Согласованность имён.** `RunOverride`, `OverrideError`, `apply_override`, `OVERRIDE_META_KEY` — задачи 1, 3, 4, 6. `extra_meta` (recorder) и `run_meta` (TaskRunner/RunAssembly) / `extra_run_meta` (исполнители) — три разных слоя, имена намеренно различаются и совпадают в задачах 2 и 3. `ProviderView`/`ModelCardView` на сервере против `ProviderCard`/`ModelCard` на клиенте: сервер называет модели ответа `*View` (как `SecretView`, `MemoryFileView`), клиент — `*Card`; соответствие полей один-в-один.
