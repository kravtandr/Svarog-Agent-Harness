# Ревью-фолоуапы: долги документации и структуры

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрыть шесть подтверждённых находок внешнего ревью: рассинхрон документации с кодом, дубли YAML-хелперов в CLI, нетипизированный gateway-клиент и два god-object'а.

**Архитектура:** Правки идут снизу вверх по риску — сначала документация (нулевой риск для рантайма), затем локальные рефакторинги с тестами, в конце механическое расщепление больших модулей по уже существующему в репозитории паттерну (`cli/policies.py`, `cli/remote.py` — отдельные Typer sub-app'ы, подключённые через `add_typer`). Ни одна задача не меняет поведение CLI: контракт команд и их вывод остаются прежними, что и проверяется существующими тестами.

**Tech Stack:** Python 3.12, Typer, Pydantic v2, SQLAlchemy + Alembic, pytest, ruff, mypy (strict).

## Global Constraints

- Докстринги и комментарии — на русском, в стиле окружающего кода: объясняют «почему», а не пересказывают код.
- Тесты запускаются с вычищенным окружением, иначе `SVAROG_*` из shell-rc ломает ~29 тестов:
  `env -u SVAROG_AGENT_HOME -u SVAROG_MEMORY__PATH -u SVAROG_REPO -u SVAROG_SKILLS__PATHS -u SVAROG_STORAGE__DB_PATH uv run pytest`
- Каждая задача заканчивается зелёными `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` и полным прогоном тестов.
- Baseline на момент написания плана: **922 passed, 1 skipped**; `main` = `19d3e71`.
- Ветка: `chore/review-followups` от `main`. Не работать на `main`.
- Публичные ADR — источник истины по архитектуре. Если код разошёлся с ADR, чинится либо код, либо ADR явной записью об отклонении; молча оставлять расхождение нельзя.

---

## Что проверено и что нет

Перед исполнением: находка №1 внешнего ревью («BridgeControl не принимает `self_docs`, 2 красных теста») **опровергнута** на `19d3e71` — параметр есть в `bridge_control.py:102`, `tests/test_self_docs.py` + `tests/test_bridge.py` дают 42 passed. Задачи под неё в плане нет намеренно. Находка №4 (нет ADR для Dream) закрыта в `main` документом `docs/adr/0020-memory-proposals-and-dream.md`.

---

### Task 1: Общий хелпер project-config вместо двух копий

Находка №7. `_read_project_config` и `_write_yaml` продублированы в `cli/policies.py` и `cli/chat_settings.py`. Копии **не идентичны**: различаются тип исключения (`typer.BadParameter` против `SettingsApplyError`) и сигнатура записи (`Mapping` против `dict`). Разница неслучайна — `chat_settings` вызывается из TUI, где `typer.BadParameter` неуместен. Поэтому общий модуль бросает нейтральное исключение, а вызывающие переводят его в своё.

**Files:**
- Create: `src/svarog_harness/common/project_config.py`
- Modify: `src/svarog_harness/cli/policies.py:22-34` (удалить `_read_project_config`), `:47-55` (удалить `_write_yaml`), `:73`, `:133` (вызовы)
- Modify: `src/svarog_harness/cli/chat_settings.py:34-46` (удалить `_read_project_config`), `:48-56` (удалить `_write_yaml`), `:60`, `:62` (вызовы)
- Test: `tests/test_project_config.py`

**Interfaces:**
- Consumes: ничего из других задач.
- Produces:
  ```python
  class ProjectConfigError(ValueError): ...
  def read_project_config(path: Path) -> dict[str, Any]: ...
  def write_yaml(path: Path, data: Mapping[str, Any]) -> None: ...
  ```

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_project_config.py`:

```python
"""Общий хелпер project-config `svarog.yaml`.

Модуль нейтрален к вызывающему: бросает ProjectConfigError, а CLI и TUI
переводят его в своё исключение (typer.BadParameter / SettingsApplyError).
"""

from pathlib import Path

import pytest

from svarog_harness.common.project_config import (
    ProjectConfigError,
    read_project_config,
    write_yaml,
)


def test_missing_file_gives_empty_mapping(tmp_path: Path) -> None:
    assert read_project_config(tmp_path / "нет.yaml") == {}


def test_empty_file_gives_empty_mapping(tmp_path: Path) -> None:
    path = tmp_path / "svarog.yaml"
    path.write_text("", encoding="utf-8")
    assert read_project_config(path) == {}


def test_reads_mapping(tmp_path: Path) -> None:
    path = tmp_path / "svarog.yaml"
    path.write_text("runtime:\n  autonomy: yolo\n", encoding="utf-8")
    assert read_project_config(path) == {"runtime": {"autonomy": "yolo"}}


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "svarog.yaml"
    path.write_text("runtime: [незакрытый\n", encoding="utf-8")
    with pytest.raises(ProjectConfigError):
        read_project_config(path)


def test_non_mapping_top_level_raises(tmp_path: Path) -> None:
    path = tmp_path / "svarog.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ProjectConfigError):
        read_project_config(path)


def test_write_is_atomic_and_leaves_no_temp(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "svarog.yaml"
    write_yaml(path, {"memory": {"path": "./память"}})
    assert read_project_config(path) == {"memory": {"path": "./память"}}
    # Атомарность: временный файл не остаётся даже при вложенном каталоге.
    assert [p.name for p in path.parent.iterdir()] == ["svarog.yaml"]


def test_write_keeps_unicode_and_order(tmp_path: Path) -> None:
    path = tmp_path / "svarog.yaml"
    write_yaml(path, {"b": "тест", "a": 1})
    text = path.read_text(encoding="utf-8")
    assert "тест" in text
    assert text.index("b:") < text.index("a:")
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `env -u SVAROG_AGENT_HOME -u SVAROG_MEMORY__PATH -u SVAROG_REPO -u SVAROG_SKILLS__PATHS -u SVAROG_STORAGE__DB_PATH uv run pytest tests/test_project_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.common.project_config'`

- [ ] **Step 3: Реализовать модуль**

Создать `src/svarog_harness/common/project_config.py`:

```python
"""Чтение и атомарная запись project-config `svarog.yaml`.

Общий для CLI (`svarog policies configure`) и TUI (`/set` в чате). Модуль
не знает про Typer: бросает ProjectConfigError, вызывающий переводит его в
своё исключение — typer.BadParameter в CLI, SettingsApplyError в чате, где
typer-ошибка всплыла бы наружу трейсбеком.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


class ProjectConfigError(ValueError):
    """Конфиг проекта нечитаем: битый YAML или не mapping на верхнем уровне."""


def read_project_config(path: Path) -> dict[str, Any]:
    """Вернуть исходный project-config, не смешивая его с user-config."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProjectConfigError(f"невалидный YAML в {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ProjectConfigError(f"{path}: верхний уровень должен быть mapping")
    return data


def write_yaml(path: Path, data: Mapping[str, Any]) -> None:
    """Атомарно сохранить YAML, чтобы Ctrl+C не оставил усечённый конфиг."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    temporary.replace(path)
```

- [ ] **Step 4: Прогнать тест — должен пройти**

Run: `env -u SVAROG_AGENT_HOME -u SVAROG_MEMORY__PATH -u SVAROG_REPO -u SVAROG_SKILLS__PATHS -u SVAROG_STORAGE__DB_PATH uv run pytest tests/test_project_config.py -q`
Expected: PASS, 7 passed

- [ ] **Step 5: Переключить `cli/policies.py` на общий модуль**

Удалить из `cli/policies.py` функции `_read_project_config` (строки 22–34) и `_write_yaml` (строки 47–55). Добавить импорт:

```python
from svarog_harness.common.project_config import (
    ProjectConfigError,
    read_project_config,
    write_yaml,
)
```

Заменить вызов на строке 73:

```python
    try:
        raw_config = read_project_config(config_path)
    except ProjectConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
```

Заменить вызов на строке 133: `_write_yaml(config_path, raw_config)` → `write_yaml(config_path, raw_config)`.

Убрать ставшие лишними импорты `yaml` и `Mapping`, если они больше нигде в файле не используются (проверит ruff).

- [ ] **Step 6: Переключить `cli/chat_settings.py` на общий модуль**

Удалить `_read_project_config` (строки 34–46) и `_write_yaml` (строки 48–56). Добавить импорт:

```python
from svarog_harness.common.project_config import (
    ProjectConfigError,
    read_project_config,
    write_yaml,
)
```

Заменить строки 60–62:

```python
    try:
        raw = read_project_config(path)
    except ProjectConfigError as exc:
        raise SettingsApplyError(str(exc)) from exc
    merged = _deep_merge(raw, patch)
    write_yaml(path, merged)
```

(имя переменной `merged` и вызов `_deep_merge` — как в текущем коде, менять не нужно)

- [ ] **Step 7: Прогнать полный набор тестов**

Run: `env -u SVAROG_AGENT_HOME -u SVAROG_MEMORY__PATH -u SVAROG_REPO -u SVAROG_SKILLS__PATHS -u SVAROG_STORAGE__DB_PATH uv run pytest -q`
Expected: PASS, 929 passed, 1 skipped (922 + 7 новых)

- [ ] **Step 8: Линтеры и типы**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: три зелёных прогона, без замечаний

- [ ] **Step 9: Коммит**

```bash
git add src/svarog_harness/common/project_config.py \
        src/svarog_harness/cli/policies.py \
        src/svarog_harness/cli/chat_settings.py \
        tests/test_project_config.py
git commit -m "refactor(cli): общий хелпер project-config вместо двух копий"
```

---

### Task 2: Типизировать gateway-клиент моделями из `gateway/models.py`

Находка №8. В `cli/remote.py` 25 вхождений `dict[str, Any]` и `cast` поверх голого JSON. При этом контракт уже описан Pydantic-моделями в `gateway/models.py` — клиент и сервер должны делить один контракт, иначе дрейф API ловится только в рантайме. Проверенная цифра — 25, не 28: ревью её завысило.

**Files:**
- Modify: `src/svarog_harness/cli/remote.py:66-70` (`_json`/`_json_list` → generic-парсинг), `:71-120` (методы runs), остальные методы клиента ниже по файлу
- Test: `tests/test_remote_cli.py` (существующий; уточнить имя командой из Step 1)

**Interfaces:**
- Consumes: `svarog_harness.gateway.models` — `RunSummary`, `RunDetail`, `ApprovalView`, `WorkspaceView`, `DirListing`, `RunDiffView`, `CancelView`, `WhoamiView`, `SessionView`, `RunRef`.
- Produces: методы `RemoteClient` возвращают Pydantic-модели вместо `dict`. Точки доступа в командах меняются с `row["field"]` на `row.field`.

- [ ] **Step 1: Найти существующие тесты клиента**

Run: `grep -rln "RemoteClient\|remote_app" tests/`
Записать список — эти файлы придётся править вместе с клиентом, они и есть страховка от регрессии.

- [ ] **Step 2: Написать падающий тест на типизированный ответ**

Добавить в найденный файл тестов (или создать `tests/test_remote_typed.py`, если отдельного файла клиента нет):

```python
def test_get_run_returns_model(monkeypatch) -> None:
    """Клиент отдаёт модель контракта, а не голый dict: дрейф полей
    ловится валидацией здесь, а не KeyError в команде."""
    from svarog_harness.cli.remote import RemoteClient
    from svarog_harness.gateway.models import RunDetail

    payload = {
        "id": "run-1",
        "status": "succeeded",
        "task": "тест",
        "created_at": "2026-07-23T10:00:00Z",
    }
    client = RemoteClient(base_url="http://x")
    monkeypatch.setattr(RemoteClient, "_request", lambda self, *a, **k: payload)

    result = client.get_run("run-1")
    assert isinstance(result, RunDetail)
    assert result.id == "run-1"
```

Перед написанием **обязательно** прочитать `src/svarog_harness/gateway/models.py` целиком и подставить в `payload` ровно те поля, которые модель требует — иначе тест упадёт на валидации, а не на типе.

- [ ] **Step 3: Прогнать тест — должен упасть**

Run: `env -u SVAROG_AGENT_HOME -u SVAROG_MEMORY__PATH -u SVAROG_REPO -u SVAROG_SKILLS__PATHS -u SVAROG_STORAGE__DB_PATH uv run pytest tests/test_remote_typed.py -q`
Expected: FAIL — `assert isinstance(result, RunDetail)`, потому что вернулся `dict`

- [ ] **Step 4: Заменить `_json`/`_json_list` на generic-парсеры**

В `cli/remote.py` заменить строки 66–70:

```python
    def _model[M: BaseModel](self, model: type[M], method: str, path: str, **kwargs: Any) -> M:
        """Разобрать ответ моделью контракта gateway: дрейф API падает здесь."""
        return model.model_validate(self._request(method, path, **kwargs))

    def _models[M: BaseModel](
        self, model: type[M], method: str, path: str, **kwargs: Any
    ) -> list[M]:
        raw = self._request(method, path, **kwargs)
        return [model.model_validate(item) for item in raw]
```

Добавить импорт `from pydantic import BaseModel` и импорты моделей из `svarog_harness.gateway.models`. Убрать `cast` из импортов `typing`, если он больше не нужен.

- [ ] **Step 5: Перевести методы клиента на модели**

Пройти по всем методам `RemoteClient` и заменить возвращаемый тип. Пример для блока runs (строки 71–105):

```python
    def create_run(
        self,
        task: str,
        *,
        autonomy: str | None = None,
        repo_url: str | None = None,
        ref: str | None = None,
        workspace: str | None = None,
    ) -> RunRef:
        payload: dict[str, Any] = {"task": task}
        if autonomy:
            payload["autonomy"] = autonomy
        if repo_url:
            payload["repo"] = {"url": repo_url, **({"ref": ref} if ref else {})}
        if workspace:
            payload["workspace"] = workspace
        return self._model(RunRef, "POST", "/runs", json=payload)

    def get_run(self, run_id: str) -> RunDetail:
        return self._model(RunDetail, "GET", f"/runs/{run_id}")

    def list_runs(self, limit: int = 20) -> list[RunSummary]:
        return self._models(RunSummary, "GET", "/runs", params={"limit": limit})

    def resume(self, run_id: str) -> RunRef:
        return self._model(RunRef, "POST", f"/runs/{run_id}/resume")

    def cancel(self, run_id: str) -> CancelView:
        return self._model(CancelView, "POST", f"/runs/{run_id}/cancel")

    def diff(self, run_id: str) -> RunDiffView:
        return self._model(RunDiffView, "GET", f"/runs/{run_id}/diff")
```

Сверять каждую пару «эндпоинт → модель» с `gateway/api.py`: `response_model` роутов — источник истины. `stream_events` остаётся `Iterator[dict[str, Any]]`: NDJSON-события не имеют единой модели.

- [ ] **Step 6: Поправить команды, читающие поля**

`mypy` покажет каждое место, где команда индексирует результат как словарь. Заменить `run["status"]` → `run.status` и т.п. Не менять формат вывода: тексты и колонки таблиц остаются прежними.

Run: `uv run mypy src`
Итеративно править, пока не станет чисто.

- [ ] **Step 7: Проверить, что `Any` осталось только на транспорте**

Run: `grep -c 'dict\[str, Any\]' src/svarog_harness/cli/remote.py`
Expected: не больше 3 (тело запроса `payload`, `**kwargs` транспорта, `stream_events`)

- [ ] **Step 8: Полный прогон и линтеры**

Run: `env -u SVAROG_AGENT_HOME -u SVAROG_MEMORY__PATH -u SVAROG_REPO -u SVAROG_SKILLS__PATHS -u SVAROG_STORAGE__DB_PATH uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: всё зелёное

- [ ] **Step 9: Коммит**

```bash
git add src/svarog_harness/cli/remote.py tests/
git commit -m "refactor(cli): remote-клиент типизирован моделями контракта gateway"
```

---

### Task 3: Привести `docs/repo-structure.md` в соответствие с деревом

Находка №3, подтверждена и шире заявленного. Расхождение в обе стороны:

| В доке | В коде |
| --- | --- |
| нет `scheduler/` | `src/svarog_harness/scheduler/` есть (ADR-0019) |
| нет `tenant/` | `src/svarog_harness/tenant/` есть (ADR-0012/13/14) |
| нет `common/` | `src/svarog_harness/common/` есть (`frontmatter.py`, после Task 1 — ещё и `project_config.py`) |
| top-level `curator/` | такого пакета нет: есть `skills/curator/` и `memory/curator.py` |
| top-level `agents/` | такого пакета нет: есть `runtime/agents/` |
| `adr/ # ADR-0001…0010` | реально 0001…0020 |

**Files:**
- Modify: `docs/repo-structure.md` — строка 11 (диапазон ADR), дерево пакетов, строка 149 (список pluggable-интерфейсов)

- [ ] **Step 1: Снять эталон реального дерева**

```bash
ls -d src/svarog_harness/*/ | xargs -n1 basename | grep -v __pycache__ | sort
find src/svarog_harness/scheduler src/svarog_harness/tenant src/svarog_harness/common -name '*.py' | sort
```

- [ ] **Step 2: Исправить диапазон ADR в строке 11**

`adr/                    # ADR-0001…0010` → `adr/                    # ADR-0001…0020`

- [ ] **Step 3: Убрать несуществующие пакеты**

Заменить top-level `curator/` на ссылку с места, где он реально живёт (внутри блока `skills/` — `curator/`, и `memory/curator.py`). Аналогично `agents/` перенести внутрь блока `runtime/` как `agents/`. Ничего не выдумывать: описание каждого модуля брать из его докстринга.

- [ ] **Step 4: Добавить недостающие пакеты**

Дописать в дерево три блока, с однострочным описанием каждого модуля по его докстрингу и ссылкой на ADR:

```
    scheduler/              # (ADR-0019)
      ...                   # по одному модулю на строку, описание из докстринга

    tenant/                 # (ADR-0012, ADR-0013, ADR-0014)
      ...

    common/                 # нейтральные утилиты без зависимостей от домена
      frontmatter.py        # YAML-frontmatter: разбор и сборка markdown (§7, ADR-0011)
      project_config.py     # чтение и атомарная запись project-config svarog.yaml
```

`project_config.py` появляется только после Task 1 — если Task 3 исполняется раньше, строку не добавлять.

- [ ] **Step 5: Проверить, что дока и дерево сошлись**

```bash
diff <(grep -oP '^    \K[a-z_]+(?=/)' docs/repo-structure.md | sort -u) \
     <(ls -d src/svarog_harness/*/ | xargs -n1 basename | grep -v __pycache__ | sort)
```
Expected: пустой вывод

- [ ] **Step 6: Коммит**

```bash
git add docs/repo-structure.md
git commit -m "docs(structure): дерево пакетов синхронизировано с кодом"
```

---

### Task 4: Актуализировать диапазоны ADR и статусы ADR-0012/13/14

Находка №5, подтверждена; мест больше, чем в ревью. Реальный максимум — ADR-0020.

| Файл | Строка | Сейчас | Должно быть |
| --- | --- | --- | --- |
| `TASK.md` | 1662 | ADR-0001…0010 | ADR-0001…0020 |
| `AGENTS.md` | 18 | ADR-0001…0018 | ADR-0001…0020 |
| `README.md` | 167 | ADR-0001…0017 | ADR-0001…0020 |
| `README.md` | 303 | ADR-0001…0017 | ADR-0001…0020 |

(строка 11 `docs/repo-structure.md` закрывается в Task 3)

Отдельно: ADR-0012, 0013, 0014 в поле статуса ссылаются на ветку `feat/multi-tenancy`, которой в репозитории нет. Код при этом **на месте** — `src/svarog_harness/tenant/` в `main`, так что фича не потеряна; устарела формулировка статуса.

**Files:**
- Modify: `TASK.md:1662`, `AGENTS.md:18`, `README.md:167`, `README.md:303`
- Modify: `docs/adr/0012-multi-tenancy.md:5`, `docs/adr/0013-privilege-levels.md:5`, `docs/adr/0014-multi-tenant-integration.md:5`

- [ ] **Step 1: Обновить диапазоны**

В каждой из четырёх строк заменить верхнюю границу на `0020`. В `README.md:167` и `:303` дополнить перечень в скобках свежими ADR: `чат-TUI — 0018, планировщик — 0019, memory-proposals и Dream — 0020`.

- [ ] **Step 2: Заменить ссылку на несуществующую ветку в ADR-0012**

`docs/adr/0012-multi-tenancy.md`, строка 5:
`Принято (реализовано в ветке `feat/multi-tenancy`)` → `Принято и реализовано (`src/svarog_harness/tenant/`)`

- [ ] **Step 3: То же в ADR-0013**

`docs/adr/0013-privilege-levels.md`, строка 5:
`Принято (реализовано в ветке `feat/multi-tenancy`)` → `Принято и реализовано (`src/svarog_harness/tenant/`)`

- [ ] **Step 4: То же в ADR-0014**

`docs/adr/0014-multi-tenant-integration.md`, строка 5:
`Принято (Фазы 1–3 реализованы в ветке `feat/multi-tenancy`)` → `Принято, фазы 1–3 реализованы (`src/svarog_harness/tenant/`, `gateway/hub.py`)`

- [ ] **Step 5: Проверить, что мёртвых ссылок не осталось**

```bash
grep -rn "в ветке \`feat/" docs/ README.md AGENTS.md TASK.md
grep -rn "ADR-0001…00\(0[0-9]\|1[0-9]\)\b" docs/ README.md AGENTS.md TASK.md
```
Expected: оба — пустой вывод

- [ ] **Step 6: Коммит**

```bash
git add TASK.md AGENTS.md README.md docs/adr/0012-multi-tenancy.md \
        docs/adr/0013-privilege-levels.md docs/adr/0014-multi-tenant-integration.md
git commit -m "docs: актуальные диапазоны ADR и статусы мультиарендности"
```

---

### Task 5: Зафиксировать отклонение от ADR-0008 по `QueueBackend` и `PolicyEngine`

Находка №2. `AGENTS.md:30` и `ADR-0008:44` требуют закладывать интерфейсы `ModelProvider`, `SandboxBackend`, `QueueBackend`, `SecretStore`, `PolicyEngine` даже при единственной реализации. Проверено:

- `ModelProvider` — есть (`llm/provider.py:97`, ABC) ✔
- `SecretStore` — есть (`secrets/store.py:16`, ABC) ✔
- `SandboxBackend` — есть под именем `ExecutionEnvironment` (`sandbox/base.py:68`, ABC) ✔, но **имя в доке другое**
- `QueueBackend` — отсутствует. `docs/repo-structure.md:115` уже фиксирует это осознанно: «QueueBackend отдельным модулем нет — очередь памяти = таблица `memory_queue`». Противоречие внутри самой документации.
- `PolicyEngine` — конкретный класс (`policy/engine.py:67`), не ABC/Protocol.

Дополнительно найдено при проверке: таблица `memory_queue` объявлена в `storage/models.py` и в начальной миграции, но **ни один модуль её не читает и не пишет** — обоснование «очередь = таблица memory_queue» опирается на мёртвый код.

Рекомендация — YAGNI: не изобретать `QueueBackend` под таблицу, которой никто не пользуется, а записать отклонение явно. Выбор между «задокументировать отклонение» и «реализовать интерфейс» — архитектурный, за владельцем репозитория. План написан под рекомендуемый вариант.

**Files:**
- Modify: `docs/adr/0008-mvp-scope.md` (добавить раздел с отклонениями)
- Modify: `AGENTS.md:30` (привести список к реальности)
- Modify: `docs/repo-structure.md:115` (уточнить формулировку)
- Modify: `src/svarog_harness/storage/models.py` (комментарий у `memory_queue`)

- [ ] **Step 1: Подтвердить, что `memory_queue` мёртв**

```bash
grep -rn "memory_queue\|MemoryQueue" --include='*.py' src/ tests/ | grep -v migrations
```
Expected: только объявление в `storage/models.py`. Если найдётся чтение или запись — остановиться и пересмотреть задачу: обоснование в доке тогда верно, и менять надо только формулировку про `QueueBackend`.

- [ ] **Step 2: Дописать раздел отклонений в ADR-0008**

В конец `docs/adr/0008-mvp-scope.md` добавить:

```markdown
## Отклонения по состоянию на 2026-07-23

Требование §«интерфейсы закладываются в MVP» выполнено для `ModelProvider`
(`llm/provider.py`), `SecretStore` (`secrets/store.py`) и sandbox-бэкенда —
последний носит имя `ExecutionEnvironment` (`sandbox/base.py`), а не
`SandboxBackend`.

Два интерфейса сознательно не заведены:

* **`QueueBackend`** — второй реализации не появилось, а единственный
  потребитель абстракции отсутствует: таблица `memory_queue` объявлена в
  `storage/models.py`, но кода, который в неё пишет или читает, в репозитории
  нет. Заводить интерфейс под неиспользуемое хранилище — спекулятивное
  обобщение. Появится реальная очередь (Redis для cloud-режима, ADR-0017) —
  интерфейс вводится вместе со второй реализацией.
* **`PolicyEngine`** — остаётся конкретным классом (`policy/engine.py`).
  Enforcement по ADR-0002 един для всех развёртываний; подменяемая политика
  сделала бы границу безопасности расширяемой, что противоречит ADR-0002.

Пересмотреть при появлении второй реализации любого из двух.
```

- [ ] **Step 3: Привести `AGENTS.md:30` к реальности**

Заменить строку 30:

```markdown
4. Интерфейсы (`ModelProvider`, `ExecutionEnvironment`, `SecretStore` и т.д.) закладывать сразу, даже при единственной реализации (ADR-0008; отклонения по `QueueBackend` и `PolicyEngine` — в разделе «Отклонения» ADR-0008).
```

- [ ] **Step 4: Уточнить `docs/repo-structure.md:115`**

Заменить строку 115:

```
      # QueueBackend отдельным модулем нет: очереди как рантайм-механизма
      # тоже нет — таблица memory_queue объявлена, но не используется
      # (ADR-0008, «Отклонения»)
```

Строку 149 того же файла (перечень pluggable-интерфейсов) привести к списку из Step 3.

- [ ] **Step 5: Пометить мёртвую таблицу в коде**

В `src/svarog_harness/storage/models.py` над объявлением `memory_queue` добавить комментарий:

```python
# Наследие первоначальной схемы: таблица не используется ни одним модулем.
# Сносить миграцией дороже, чем оставить, — но новую очередь строить не на
# ней, а вместе с явным QueueBackend (ADR-0008, «Отклонения»).
```

Таблицу и миграцию **не трогать**: дропать её потребовало бы новой миграции ради нулевой выгоды.

- [ ] **Step 6: Прогон тестов (комментарий не должен ничего сломать)**

Run: `env -u SVAROG_AGENT_HOME -u SVAROG_MEMORY__PATH -u SVAROG_REPO -u SVAROG_SKILLS__PATHS -u SVAROG_STORAGE__DB_PATH uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: зелено

- [ ] **Step 7: Коммит**

```bash
git add docs/adr/0008-mvp-scope.md AGENTS.md docs/repo-structure.md \
        src/svarog_harness/storage/models.py
git commit -m "docs(adr-0008): зафиксированы отклонения по QueueBackend и PolicyEngine"
```

---

### Task 6: Расщепить `cli/main.py` по sub-app'ам

Находка №6a. `cli/main.py` — 2648 строк (в прошлое ревью было 2484). В файле девять Typer sub-app'ов, объявленных прямо в нём: `traces`, `sessions`, `approvals`, `skills` (+вложенный `proposals`), `cron`, `memory` (+вложенный `proposals`), `tenant`, `mcp`, `secrets`.

Паттерн выноса в репозитории уже есть и работает: `cli/policies.py` и `cli/remote.py` объявляют свой `*_app` и подключаются в `main.py` через `app.add_typer`. Задача — распространить его на остальные группы. Поведение CLI не меняется ни в одном символе вывода.

**Files:**
- Create: `src/svarog_harness/cli/_shared.py` (общие хелперы)
- Create: `src/svarog_harness/cli/cron_commands.py`, `memory_commands.py`, `tenant_commands.py`, `skills_commands.py`, `approvals_commands.py`, `traces_commands.py`, `secrets_commands.py`, `mcp_commands.py`
- Modify: `src/svarog_harness/cli/main.py`
- Test: существующие CLI-тесты — страховка, новых не требуется

**Interfaces:**
- Produces: каждый модуль экспортирует один `*_app: typer.Typer`. `main.py` подключает их через `app.add_typer(<module>.<name>_app, name="<группа>")`.
- `_shared.py` экспортирует хелперы, нужные больше чем одной группе — их точный состав определяется в Step 2, не угадывается заранее.

**Порядок важен: по одной группе за коммит.** Каждая перенесённая группа отдельно проверяется тестами. Ниже развёрнут первый перенос (`cron`); остальные делаются по тому же шаблону.

- [ ] **Step 1: Зафиксировать эталон поверхности CLI**

```bash
uv run svarog --help > /tmp/cli-help-before.txt
for g in traces sessions approvals skills cron memory tenant mcp secrets policies remote; do
  echo "=== $g ==="; uv run svarog "$g" --help
done >> /tmp/cli-help-before.txt
wc -l /tmp/cli-help-before.txt
```
Этот файл — критерий приёмки всей задачи: после каждого переноса вывод обязан совпадать байт в байт.

- [ ] **Step 2: Вынести общие хелперы в `_shared.py`**

Определить, какие приватные функции `main.py` понадобятся выносимым модулям:

```bash
grep -n "^def _" src/svarog_harness/cli/main.py
```

Кандидаты, используемые несколькими группами: `_load_config_or_exit` (617), `_console_hooks` (660), `_known_secret_values` (640), `_show_approval` (1073), `_report_outcome` (1217). Точный список — по фактическому использованию в переносимом коде, не по догадке.

Создать `src/svarog_harness/cli/_shared.py` с докстрингом:

```python
"""Хелперы, общие для команд CLI.

Вынесены из main.py при расщеплении по sub-app'ам: каждая группа команд
живёт в своём модуле (паттерн cli/policies.py), а общее — здесь, чтобы
модули групп не импортировали main.py и не создавали цикл.
"""
```

Перенести выбранные функции **без изменений тела**, переименовав из `_name` в `name` (они становятся внутренним API пакета, а не приватными для модуля). В `main.py` заменить определения на импорт.

- [ ] **Step 3: Прогнать тесты после выноса хелперов**

Run: `env -u SVAROG_AGENT_HOME -u SVAROG_MEMORY__PATH -u SVAROG_REPO -u SVAROG_SKILLS__PATHS -u SVAROG_STORAGE__DB_PATH uv run pytest -q`
Expected: тот же счёт, что и до задачи

- [ ] **Step 4: Коммит хелперов**

```bash
git add src/svarog_harness/cli/_shared.py src/svarog_harness/cli/main.py
git commit -m "refactor(cli): общие хелперы команд вынесены в _shared"
```

- [ ] **Step 5: Перенести группу `cron`**

Создать `src/svarog_harness/cli/cron_commands.py`. Перенести из `main.py` строки 1761–1954 целиком: объявление `cron_app` (1761), `_job_row` (1765) и команды `add`/`list`/`enable`/`disable`/`show`/`remove` вместе с `_cron_toggle` (1894). Добавить докстринг модуля:

```python
"""Команды `svarog cron`: джобы планировщика (ADR-0019)."""
```

Импорты перенести те, что нужны этим командам; недостающие общие — из `cli._shared`.

В `main.py` удалить перенесённое и заменить на:

```python
from svarog_harness.cli import cron_commands

app.add_typer(cron_commands.cron_app, name="cron")
```

Строку `app.add_typer(cron_app, name="cron")` (1762) удалить — её заменяет вызов выше.

- [ ] **Step 6: Проверить, что поверхность CLI не изменилась**

```bash
uv run svarog cron --help
diff <(uv run svarog cron --help) <(sed -n '/=== cron ===/,/=== memory ===/p' /tmp/cli-help-before.txt | sed '1d;$d')
```
Expected: пустой diff

- [ ] **Step 7: Прогон тестов**

Run: `env -u SVAROG_AGENT_HOME -u SVAROG_MEMORY__PATH -u SVAROG_REPO -u SVAROG_SKILLS__PATHS -u SVAROG_STORAGE__DB_PATH uv run pytest -q && uv run mypy src`
Expected: тот же счёт, mypy чист

- [ ] **Step 8: Коммит группы**

```bash
git add src/svarog_harness/cli/cron_commands.py src/svarog_harness/cli/main.py
git commit -m "refactor(cli): команды cron вынесены в отдельный модуль"
```

- [ ] **Step 9: Повторить Steps 5–8 для остальных групп**

По одной группе за итерацию, в порядке возрастания связности с остальным кодом:

| Группа | Строки в `main.py` | Новый модуль |
| --- | --- | --- |
| `mcp` | 2579–2617 | `mcp_commands.py` |
| `secrets` | 2618–2648 | `secrets_commands.py` |
| `tenant` | 2269–2383 | `tenant_commands.py` |
| `traces` + `sessions` | 1269–1364 | `traces_commands.py` |
| `approvals` | 1420–1496 (+ `_decide_approval_command`, `_show_approval` уже в `_shared`) | `approvals_commands.py` |
| `memory` + вложенный `proposals` | 2078–2268 | `memory_commands.py` |
| `skills` + вложенный `proposals` | 1497–1760 | `skills_commands.py` |

Номера строк — на момент написания плана; после каждого переноса они сдвигаются. Границы группы определять по `grep -n "^@<группа>_app\.\|^<группа>_app = typer.Typer"`, а не по абсолютным номерам.

Команды верхнего уровня (`version`, `doctor`, `init`, `run`, `chat`, `resume`, `rewind`, `scheduler`, `push`, `serve`, `telegram`) **остаются в `main.py`** — это точка входа, дробить её дальше смысла нет.

- [ ] **Step 10: Проверить итоговый размер и полную поверхность CLI**

```bash
wc -l src/svarog_harness/cli/main.py
uv run svarog --help > /tmp/cli-help-after.txt
for g in traces sessions approvals skills cron memory tenant mcp secrets policies remote; do
  echo "=== $g ==="; uv run svarog "$g" --help
done >> /tmp/cli-help-after.txt
diff /tmp/cli-help-before.txt /tmp/cli-help-after.txt
```
Expected: `main.py` ≤ 1700 строк, diff пустой

- [ ] **Step 11: Финальный прогон**

Run: `env -u SVAROG_AGENT_HOME -u SVAROG_MEMORY__PATH -u SVAROG_REPO -u SVAROG_SKILLS__PATHS -u SVAROG_STORAGE__DB_PATH uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: всё зелёное, счёт тестов не изменился

- [ ] **Step 12: Обновить дерево в `docs/repo-structure.md`**

Дописать новые модули `cli/` в блок структуры (по одной строке на модуль, описание из докстринга) и закоммитить:

```bash
git add docs/repo-structure.md
git commit -m "docs(structure): модули команд cli в дереве"
```

---

### Task 7: Расщепить `TaskRunner`

Находка №6b. `runtime/orchestrator.py` — 1389 строк (было 1320), `TaskRunner` совмещает несколько ролей: сборку окружения и исполнителя (`build_environment`, `build_agent_infra`, `build_loop`, `build_external_executor`, `wire_bridge_control`, `prepare_agent_launch`, `_build_registry`), собственно исполнение (`run_once`, `resume`, `_resume_external`, `spawn_child_run`) и пост-обработку (`verify`, `drain_memory`, `drain_schedule`, `drain_proposals`, `drain_memory_proposals`, `_autocommit`).

Естественный шов — между сборкой и исполнением: методы `build_*`/`wire_*` не трогают состояние run'а, только конструируют объекты из конфига.

**Риск выше, чем у Task 6:** `TaskRunner` — сердце рантайма, его покрытие тестами косвенное (через сценарии). Задачу имеет смысл брать только после того, как Tasks 1–6 влиты и зелены, и **разделять по одному шву за коммит**, чтобы откат был дешёвым. Если после первого шва тесты нестабильны — остановиться и не продолжать: остальные находки от этого не зависят.

**Files:**
- Create: `src/svarog_harness/runtime/run_assembly.py`
- Modify: `src/svarog_harness/runtime/orchestrator.py`
- Test: существующие тесты рантайма

**Interfaces:**
- Produces: `RunAssembly` — держатель конфига/окружения со сборочными методами; `TaskRunner` делегирует ему, публичная сигнатура `TaskRunner` не меняется.

- [ ] **Step 1: Зафиксировать baseline рантайм-тестов**

```bash
env -u SVAROG_AGENT_HOME -u SVAROG_MEMORY__PATH -u SVAROG_REPO -u SVAROG_SKILLS__PATHS -u SVAROG_STORAGE__DB_PATH \
  uv run pytest tests/ -q -k "runner or orchestrator or external or loop" 2>&1 | tail -3
```
Записать счёт — он не должен измениться ни на одном шаге.

- [ ] **Step 2: Составить карту зависимостей сборочных методов**

```bash
sed -n '240,700p' src/svarog_harness/runtime/orchestrator.py | grep -n "self\._" | sort -u -t: -k2 | head -40
```
Выписать, к каким полям `self._*` обращаются `build_environment`, `build_agent_infra`, `build_loop`, `build_external_executor`, `wire_bridge_control`, `prepare_agent_launch`, `_build_registry`, `_defer_mcp_schemas`, `known_secret_values`. Именно этот набор станет полями `RunAssembly`.

Если хотя бы один сборочный метод пишет в поле, которое читает исполняющая часть, — шов проходит не здесь. Тогда остановиться и вынести вместо этого пост-обработку (`drain_*`), она изолирована лучше.

- [ ] **Step 3: Создать `run_assembly.py` с перенесёнными методами**

```python
"""Сборка исполнителя run'а: окружение, executor, реестр tools, мост.

Вынесено из TaskRunner (orchestrator.py): эти методы не трогают состояние
run'а — только конструируют объекты из конфига, поэтому живут отдельно от
исполняющей и пост-обрабатывающей частей.
"""
```

Перенести тела методов **дословно**, заменив обращения к полям `TaskRunner` на поля `RunAssembly`, установленные в его `__init__` из Step 2.

- [ ] **Step 4: Делегировать из `TaskRunner`**

В `TaskRunner.__init__` создать `self._assembly = RunAssembly(...)`. Каждый перенесённый метод заменить на однострочное делегирование, например:

```python
    def build_environment(self, infra: ExternalAgentInfra | None = None) -> ExecutionEnvironment:
        return self._assembly.build_environment(infra)
```

Публичные сигнатуры сохраняются — внешние вызывающие (`cli/main.py`, `gateway/service.py`, тесты) не меняются.

- [ ] **Step 5: Прогон рантайм-тестов**

Run: команда из Step 1
Expected: тот же счёт, что зафиксирован в Step 1

- [ ] **Step 6: Полный прогон и линтеры**

Run: `env -u SVAROG_AGENT_HOME -u SVAROG_MEMORY__PATH -u SVAROG_REPO -u SVAROG_SKILLS__PATHS -u SVAROG_STORAGE__DB_PATH uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: всё зелёное

- [ ] **Step 7: Проверить размер**

Run: `wc -l src/svarog_harness/runtime/orchestrator.py src/svarog_harness/runtime/run_assembly.py`
Expected: `orchestrator.py` ≤ 1100 строк

- [ ] **Step 8: Коммит**

```bash
git add src/svarog_harness/runtime/run_assembly.py src/svarog_harness/runtime/orchestrator.py
git commit -m "refactor(runtime): сборка исполнителя вынесена из TaskRunner"
```

- [ ] **Step 9: Обновить дерево в доке**

Дописать `run_assembly.py` в блок `runtime/` файла `docs/repo-structure.md`, закоммитить.

---

## Приёмка всего плана

- [ ] `env -u SVAROG_AGENT_HOME -u SVAROG_MEMORY__PATH -u SVAROG_REPO -u SVAROG_SKILLS__PATHS -u SVAROG_STORAGE__DB_PATH uv run pytest -q` — не меньше 929 passed, 1 skipped
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy src` — чисто
- [ ] `diff /tmp/cli-help-before.txt /tmp/cli-help-after.txt` — пусто
- [ ] `grep -rn "в ветке \`feat/" docs/ README.md AGENTS.md TASK.md` — пусто
- [ ] Дерево `docs/repo-structure.md` совпадает с `ls src/svarog_harness/*/`
- [ ] `wc -l src/svarog_harness/cli/main.py` ≤ 1700, `runtime/orchestrator.py` ≤ 1100
