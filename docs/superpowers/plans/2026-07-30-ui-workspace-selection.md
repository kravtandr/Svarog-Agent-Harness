# Выбор рабочей директории из UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Папка-корень выбирается в веб-клиенте при создании сессии; на каждый корень — свой `GatewayService` со своим конфигом (мультиплексор `WorkspaceHub`, только single-tenant).

**Architecture:** `WorkspaceHub` в `gateway/hub.py` по образцу `TenantHub`, ключ — путь. JSON-реестр `~/.svarog/workspace-roots.json` хранит недавние корни и карты `session→root` / `run→root` (кэш маршрутизации, не источник истины). API получает `path` в создании сессий/run'ов, маршрутизацию по id через `_require_service` и `GET /fs` для пикера. Клиент — экран `WorkspacePicker` (недавние + обзор + автодополнение).

**Tech Stack:** Python 3.11+/FastAPI/pydantic/SQLAlchemy (backend), React+TypeScript+Vite/vitest (web), uv/ruff/mypy.

**Спека:** `docs/superpowers/specs/2026-07-30-ui-workspace-selection-design.md`

## Global Constraints

- Комментарии и docstrings — на русском, в стиле окружающего кода (объясняют «почему»).
- Backend-проверки перед каждым коммитом: `uv run ruff format <изменённые файлы> && uv run ruff check <изменённые файлы> && uv run mypy src`.
- Web-проверки: `npm --prefix web test -- --run` (vitest); сборка — `npm --prefix web run build`.
- Тесты backend: `uv run pytest tests/<файл> -v`.
- Фича существует только в single-tenant: в multi-tenant режиме `WorkspaceHub` не создаётся, `/fs` не зарегистрирован, `path` в теле запроса → 422.
- Существующее поле `Session.meta["workspace"]` (абсолютный путь, пишется в `create_session`) — и есть «root» из спеки; новый ключ meta не заводим.
- Ошибки: несуществующий/не-каталог `path` → 422; корень сессии удалён с диска → 410; `path` вместе с `repo`/`workspace` → 422.

---

### Task 1: Реестр корней `WorkspaceRootsRegistry`

**Files:**
- Create: `src/svarog_harness/gateway/roots.py`
- Test: `tests/test_workspace_roots.py`

**Interfaces:**
- Consumes: ничего (stdlib only).
- Produces: `WorkspaceRootsRegistry(path: Path)` c методами
  `record_session(session_id: str, root: Path) -> None`,
  `record_run(run_id: str, root: Path) -> None`,
  `roots() -> list[tuple[Path, str]]` (свежие сверху, ts — ISO-строка),
  `root_of_session(session_id: str) -> Path | None`,
  `root_of_run(run_id: str) -> Path | None`,
  `roots_with_runs() -> set[Path]`.
  Каждая запись «трогает» корень (обновляет last_used) и лениво чистит
  несуществующие корни. Битый/отсутствующий файл — пустой реестр, не исключение.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_workspace_roots.py
"""Тесты реестра корней workspace-сессий (спека 2026-07-30)."""

from pathlib import Path

from svarog_harness.gateway.roots import WorkspaceRootsRegistry


def test_records_and_orders_roots(tmp_path: Path) -> None:
    reg = WorkspaceRootsRegistry(tmp_path / "reg.json")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    reg.record_session("s1", a)
    reg.record_session("s2", b)
    assert [root for root, _ in reg.roots()] == [b, a]  # свежие сверху
    assert reg.root_of_session("s1") == a
    assert reg.root_of_session("нет-такой") is None


def test_records_runs(tmp_path: Path) -> None:
    reg = WorkspaceRootsRegistry(tmp_path / "reg.json")
    root = tmp_path / "w"
    root.mkdir()
    reg.record_run("r1", root)
    assert reg.root_of_run("r1") == root
    assert reg.roots_with_runs() == {root}


def test_tolerates_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "reg.json"
    path.write_text("{оборванный json", encoding="utf-8")
    reg = WorkspaceRootsRegistry(path)
    assert reg.roots() == []
    assert reg.root_of_session("s") is None
    root = tmp_path / "w"
    root.mkdir()
    reg.record_session("s", root)  # запись лечит файл
    assert reg.root_of_session("s") == root


def test_prunes_dead_roots_on_write(tmp_path: Path) -> None:
    reg = WorkspaceRootsRegistry(tmp_path / "reg.json")
    dead, alive = tmp_path / "dead", tmp_path / "alive"
    dead.mkdir()
    alive.mkdir()
    reg.record_session("s1", dead)
    dead.rmdir()
    reg.record_session("s2", alive)  # ленивая чистка при записи
    assert [root for root, _ in reg.roots()] == [alive]
    # Карта сессий не чистится: папка может вернуться (реестр — кэш).
    assert reg.root_of_session("s1") == dead


def test_missing_file_is_empty(tmp_path: Path) -> None:
    reg = WorkspaceRootsRegistry(tmp_path / "нет" / "reg.json")
    assert reg.roots() == []
    assert reg.roots_with_runs() == set()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_workspace_roots.py -v`
Expected: FAIL — `ModuleNotFoundError: svarog_harness.gateway.roots`

- [ ] **Step 3: Реализовать реестр**

```python
# src/svarog_harness/gateway/roots.py
"""Реестр корней workspace-сессий (спека 2026-07-30).

JSON в ~/.svarog/workspace-roots.json: известные корни (для «недавних»
пикера) и карты маршрутизации session→root / run→root. Реестр — кэш
маршрутизации, а не источник истины: промах ведёт на default_root
(WorkspaceHub.route), путь сессии дублируется в Session.meta["workspace"].
Битый или отсутствующий файл — пустой реестр; следующая запись лечит его.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class WorkspaceRootsRegistry:
    path: Path

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # атомарная подмена: полузаписанный файл не читается

    def _touch_root(self, data: dict[str, Any], root: Path) -> None:
        """Обновить last_used корня; заодно лениво выкинуть исчезнувшие."""
        roots = data.setdefault("roots", {})
        for known in list(roots):
            if not Path(known).is_dir():
                del roots[known]
        roots[str(root)] = datetime.now(UTC).isoformat()

    def record_session(self, session_id: str, root: Path) -> None:
        data = self._load()
        data.setdefault("sessions", {})[session_id] = str(root)
        self._touch_root(data, root)
        self._save(data)

    def record_run(self, run_id: str, root: Path) -> None:
        data = self._load()
        data.setdefault("runs", {})[run_id] = str(root)
        self._touch_root(data, root)
        self._save(data)

    def roots(self) -> list[tuple[Path, str]]:
        """Известные корни, свежие сверху (для «недавних» пикера)."""
        items = self._load().get("roots", {})
        if not isinstance(items, dict):
            return []
        ordered = sorted(items.items(), key=lambda kv: str(kv[1]), reverse=True)
        return [(Path(p), str(ts)) for p, ts in ordered]

    def root_of_session(self, session_id: str) -> Path | None:
        value = self._load().get("sessions", {}).get(session_id)
        return Path(value) if isinstance(value, str) else None

    def root_of_run(self, run_id: str) -> Path | None:
        value = self._load().get("runs", {}).get(run_id)
        return Path(value) if isinstance(value, str) else None

    def roots_with_runs(self) -> set[Path]:
        """Корни с записанными run'ами — обход refuel-супервизора."""
        runs = self._load().get("runs", {})
        if not isinstance(runs, dict):
            return set()
        return {Path(v) for v in runs.values() if isinstance(v, str)}
```

- [ ] **Step 4: Прогнать тесты**

Run: `uv run pytest tests/test_workspace_roots.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Линт и коммит**

```bash
uv run ruff format src/svarog_harness/gateway/roots.py tests/test_workspace_roots.py
uv run ruff check src/svarog_harness/gateway/roots.py tests/test_workspace_roots.py
uv run mypy src
git add src/svarog_harness/gateway/roots.py tests/test_workspace_roots.py
git commit -m "feat(gateway): реестр корней workspace-сессий (спека 2026-07-30)"
```

---

### Task 2: `WorkspaceHub` — auth, `service_for`, маршрутизация

**Files:**
- Modify: `src/svarog_harness/gateway/hub.py` (добавить в конец файла)
- Modify: `src/svarog_harness/gateway/service.py:180` (поле `on_session_created` рядом с `on_run_created`), `service.py:762` (вызов в `create_session`)
- Test: `tests/test_workspace_hub.py`

**Interfaces:**
- Consumes: `WorkspaceRootsRegistry` (Task 1), `GatewayService`, `load_config`, `extract_bearer`.
- Produces:
  - `RootPathError(ValueError)`, `RootGoneError(LookupError)` в `hub.py`;
  - `WorkspaceHub(base_cfg, default_root, registry, bearer_token=None)` c
    `authenticate(authorization, *, query_token=None) -> GatewayService | None`,
    `service_for(path: str | Path) -> GatewayService`,
    `route(*, session_id=None, run_id=None, root=None) -> GatewayService`,
    `supervisor_enabled: bool`, `run_supervisor(...)`, `shutdown()`;
  - `GatewayService.on_session_created: Callable[[str], None] | None` — зовётся
    после создания записи сессии с её id.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_workspace_hub.py
"""Тесты WorkspaceHub: мультиплекс GatewayService по корням (спека 2026-07-30)."""

from pathlib import Path

import pytest

from svarog_harness.config.loader import load_config
from svarog_harness.gateway.hub import RootGoneError, RootPathError, WorkspaceHub
from svarog_harness.gateway.roots import WorkspaceRootsRegistry


def _write_root(root: Path, db: Path) -> None:
    """Минимальный конфиг корня; db_path — вне корня, чтобы пережить rmdir."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {db}\n",
        encoding="utf-8",
    )


@pytest.fixture()
def hub(tmp_path: Path) -> WorkspaceHub:
    default_root = tmp_path / "default"
    _write_root(default_root, tmp_path / "default.db")
    cfg = load_config(project_dir=default_root)
    registry = WorkspaceRootsRegistry(tmp_path / "roots.json")
    return WorkspaceHub(cfg, default_root, registry=registry)


def test_default_root_service_reuses_base_cfg(hub: WorkspaceHub, tmp_path: Path) -> None:
    svc = hub.service_for(tmp_path / "default")
    assert svc.cfg is hub.base_cfg  # без повторного load_config
    assert svc is hub.service_for(tmp_path / "default")  # кэш


def test_service_for_loads_root_config(hub: WorkspaceHub, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    svc = hub.service_for(other)
    assert svc.workspace == other.resolve()
    assert svc.cfg is not hub.base_cfg
    assert svc is hub.service_for(other)  # кэш по resolved-пути


def test_service_for_rejects_bad_paths(hub: WorkspaceHub, tmp_path: Path) -> None:
    with pytest.raises(RootPathError):
        hub.service_for(tmp_path / "нет-такого")
    as_file = tmp_path / "файл.txt"
    as_file.write_text("x", encoding="utf-8")
    with pytest.raises(RootPathError):
        hub.service_for(as_file)


def test_route_by_session_and_miss(hub: WorkspaceHub, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    hub.registry.record_session("s-other", other)
    assert hub.route(session_id="s-other").workspace == other.resolve()
    # Промах реестра (сессия до фичи) → сервис default_root.
    assert hub.route(session_id="s-старая").workspace == (tmp_path / "default").resolve()
    assert hub.route().workspace == (tmp_path / "default").resolve()


def test_route_gone_root_is_410(hub: WorkspaceHub, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    hub.registry.record_session("s1", other)
    (other / "svarog.yaml").unlink()
    other.rmdir()
    with pytest.raises(RootGoneError):
        hub.route(session_id="s1")


def test_route_by_header_root(hub: WorkspaceHub, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    assert hub.route(root=str(other)).workspace == other.resolve()
    with pytest.raises(RootPathError):
        hub.route(root=str(tmp_path / "нет"))


def test_authenticate_bearer(hub: WorkspaceHub) -> None:
    assert hub.authenticate(None) is not None  # токен не настроен — открытый режим
    hub.bearer_token = "секрет"  # noqa: S105 — тестовое значение
    assert hub.authenticate(None) is None
    assert hub.authenticate("Bearer не-тот") is None
    assert hub.authenticate("Bearer секрет") is not None
    assert hub.authenticate(None, query_token="секрет") is not None
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_workspace_hub.py -v`
Expected: FAIL — `ImportError: cannot import name 'WorkspaceHub'`

- [ ] **Step 3: Добавить `on_session_created` в `GatewayService`**

В `service.py` рядом с `on_run_created` (строка ~180):

```python
    # Колбэк на создание сессии — WorkspaceHub пишет им session→root
    # в реестр маршрутизации (спека 2026-07-30).
    on_session_created: Callable[[str], None] | None = None
```

В `create_session` (строка ~776), сразу после `session = await self._read(action)`:

```python
        if self.on_session_created is not None:
            self.on_session_created(session.id)
```

- [ ] **Step 4: Реализовать `WorkspaceHub` в `hub.py`**

Добавить импорты вверху файла: `from pathlib import Path`,
`from svarog_harness.config.loader import load_config`,
`from svarog_harness.gateway.roots import WorkspaceRootsRegistry`.
В конец файла:

```python
class RootPathError(ValueError):
    """Кандидат в корень не существует или не каталог (422)."""


class RootGoneError(LookupError):
    """Корень сессии/run'а удалён с диска (410 Gone)."""


@dataclass
class WorkspaceHub:
    """Мультиплекс GatewayService по папкам-корням (спека 2026-07-30).

    Как TenantHub, но ключ — путь: каждый корень получает сервис со своим
    конфигом (`load_config(project_dir=root)`), памятью и скиллами. Auth —
    общий bearer, как в SingleTenantResolver: фича живёт только в
    single-tenant, в multi-tenant режиме хаб не создаётся вовсе.
    """

    base_cfg: SvarogConfig
    default_root: Path
    registry: WorkspaceRootsRegistry
    bearer_token: str | None = None
    _services: dict[Path, GatewayService] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.default_root = self.default_root.expanduser().resolve()
        # Сервис корня запуска — из уже загруженного конфига, без второго load.
        self._services[self.default_root] = self._make_service(self.base_cfg, self.default_root)

    def _make_service(self, cfg: SvarogConfig, root: Path) -> GatewayService:
        # Колбэки пишут карты маршрутизации; они же обновляют last_used корня.
        return GatewayService(
            cfg,
            root,
            on_run_created=lambda run_id: self.registry.record_run(run_id, root),
            on_session_created=lambda session_id: self.registry.record_session(session_id, root),
        )

    def service_for(self, path: str | Path) -> GatewayService:
        """Сервис произвольного корня; несуществующий/не-каталог — RootPathError."""
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise RootPathError(f"не каталог или не существует: {root}")
        svc = self._services.get(root)
        if svc is None:
            svc = self._make_service(load_config(project_dir=root), root)
            self._services[root] = svc
        return svc

    def route(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        root: str | None = None,
    ) -> GatewayService:
        """Сервис запроса: заголовок X-Svarog-Root → id → default_root.

        Промах реестра — default_root: сессии, созданные до фичи, работают
        без миграции. Известный, но исчезнувший корень — RootGoneError (410).
        """
        if root is not None:
            return self.service_for(root)
        target: Path | None = None
        if session_id is not None:
            target = self.registry.root_of_session(session_id)
        elif run_id is not None:
            target = self.registry.root_of_run(run_id)
        if target is None:
            return self._services[self.default_root]
        if not target.is_dir():
            raise RootGoneError(f"каталог сессии удалён: {target}")
        return self.service_for(target)

    def authenticate(
        self, authorization: str | None, *, query_token: str | None = None
    ) -> GatewayService | None:
        """Auth-гейт как у SingleTenantResolver; выбор сервиса — в route()."""
        if self.bearer_token is None:
            return self._services[self.default_root]
        token = extract_bearer(authorization) or query_token
        return self._services[self.default_root] if token == self.bearer_token else None

    @property
    def supervisor_enabled(self) -> bool:
        return self.base_cfg.supervisor.auto_resume_refuel

    async def run_supervisor(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        """Refuel-супервизор по корням с записанными run'ами (как TenantHub)."""
        interval = self.base_cfg.supervisor.interval_sec
        while should_stop is None or not should_stop():
            for root in {self.default_root, *self.registry.roots_with_runs()}:
                if not root.is_dir():
                    continue  # исчезнувший корень: run поднимется, когда папка вернётся
                with contextlib.suppress(Exception):
                    await self.service_for(root).supervise_once()
            await asyncio.sleep(interval)

    async def shutdown(self) -> None:
        """Закрыть тёплые sandbox'ы всех материализованных корней (ADR-0017)."""
        for svc in self._services.values():
            await svc.close_warm_sessions()
```

- [ ] **Step 5: Прогнать тесты**

Run: `uv run pytest tests/test_workspace_hub.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Прогнать соседей (регресс `on_session_created`)**

Run: `uv run pytest tests/test_cloud_sessions.py tests/test_gateway.py -v --no-header -q` (если `tests/test_gateway.py` нет — только cloud_sessions)
Expected: PASS

- [ ] **Step 7: Линт и коммит**

```bash
uv run ruff format src/svarog_harness/gateway/hub.py src/svarog_harness/gateway/service.py tests/test_workspace_hub.py
uv run ruff check src/svarog_harness/gateway/hub.py src/svarog_harness/gateway/service.py tests/test_workspace_hub.py
uv run mypy src
git add -A src/svarog_harness/gateway tests/test_workspace_hub.py
git commit -m "feat(gateway): WorkspaceHub — мультиплекс сервисов по корням-папкам"
```

---

### Task 3: `WorkspaceHub` — агрегированный список сессий, `/fs`-модели

**Files:**
- Modify: `src/svarog_harness/gateway/hub.py` (методы `list_sessions`, `list_fs`, `recent_roots` в `WorkspaceHub`)
- Modify: `src/svarog_harness/gateway/models.py` (модели `FsEntryView`, `FsListingView`, `RecentRootView`; поле `path` в create-запросах)
- Test: `tests/test_workspace_hub.py` (дописать)

**Interfaces:**
- Consumes: Task 2.
- Produces:
  - `WorkspaceHub.list_sessions(limit: int = 50) -> list[SessionSummary]` — веер по корням, дедуп по id;
  - `WorkspaceHub.list_fs(path: str | None) -> FsListingView`;
  - `WorkspaceHub.recent_roots() -> list[RecentRootView]`;
  - `models.FsEntryView {name, path, accessible}`, `FsListingView {path, parent, entries}`, `RecentRootView {path, exists, last_used}`;
  - `CreateSessionRequest.path: str | None`, `CreateRunRequest.path: str | None` (взаимоисключающие с `repo`/`workspace`).

- [ ] **Step 1: Дописать падающие тесты в `tests/test_workspace_hub.py`**

```python
# в конец tests/test_workspace_hub.py
import asyncio

from svarog_harness.gateway.models import CreateSessionRequest


def test_list_sessions_aggregates_and_dedups(hub: WorkspaceHub, tmp_path: Path) -> None:
    other = tmp_path / "other"
    # Общая БД с default-корнем — случай «корень без своего db_path»:
    # одна сессия видна из двух сервисов, список не должен двоиться.
    _write_root(other, tmp_path / "default.db")
    default_svc = hub.service_for(tmp_path / "default")
    other_svc = hub.service_for(other)

    async def scenario() -> list:
        await default_svc.create_session(title="в default")
        await other_svc.create_session(title="в other")
        return await hub.list_sessions()

    listed = asyncio.run(scenario())
    assert [s.title for s in listed] == ["в other", "в default"]  # свежие сверху, без дублей
    assert listed[0].workspace == str(other.resolve())


def test_list_sessions_skips_gone_roots(hub: WorkspaceHub, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    svc = hub.service_for(other)
    asyncio.run(svc.create_session(title="обречённая"))
    (other / "svarog.yaml").unlink()
    other.rmdir()
    hub._services.pop(other.resolve())  # рестарт serve: сервис не материализован
    titles = [s.title for s in asyncio.run(hub.list_sessions())]
    assert "обречённая" not in titles  # корень пропущен, а не 500


def test_list_fs_dirs_only_hidden_filtered(hub: WorkspaceHub, tmp_path: Path) -> None:
    base = tmp_path / "обзор"
    (base / "видимая").mkdir(parents=True)
    (base / ".скрытая").mkdir()
    (base / "файл.txt").write_text("x", encoding="utf-8")
    listing = hub.list_fs(str(base))
    assert [e.name for e in listing.entries] == ["видимая"]
    assert listing.path == str(base.resolve())
    assert listing.parent == str(base.resolve().parent)
    with pytest.raises(RootPathError):
        hub.list_fs(str(base / "нет-такого"))


def test_recent_roots_marks_missing(hub: WorkspaceHub, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    hub.registry.record_session("s1", other)
    (other / "svarog.yaml").unlink()
    other.rmdir()
    recents = hub.recent_roots()
    assert [(r.path, r.exists) for r in recents] == [(str(other), False)]


def test_create_requests_path_exclusive() -> None:
    with pytest.raises(ValueError, match="path"):
        CreateSessionRequest(title="x", path="/tmp", workspace="named")
    assert CreateSessionRequest(title="x", path="/tmp").path == "/tmp"
```

- [ ] **Step 2: Убедиться, что новые тесты падают**

Run: `uv run pytest tests/test_workspace_hub.py -v`
Expected: FAIL — `AttributeError: ... 'list_sessions'` / `ValidationError: path`

- [ ] **Step 3: Модели в `models.py`**

В `CreateRunRequest` после поля `workspace`:

```python
    # Абсолютный путь папки-корня (single-tenant, спека 2026-07-30);
    # взаимоисключающ с repo и workspace — это третий источник workspace.
    path: str | None = None
```

и в его валидатор `_one_workspace_source` перед `return self`:

```python
        if self.path is not None and (self.repo is not None or self.workspace is not None):
            raise ValueError("path взаимоисключающ с repo и workspace: задайте один источник")
```

То же поле и та же ветка валидатора — в `CreateSessionRequest`.
После `WorkspaceView` добавить:

```python
class FsEntryView(BaseModel):
    """Подкаталог из GET /fs (пикер рабочей папки, спека 2026-07-30)."""

    name: str
    path: str
    # Нечитаемый каталог показываем, но выбрать не даём (PermissionError).
    accessible: bool = True


class FsListingView(BaseModel):
    path: str
    parent: str | None  # None — корень ФС, выше некуда
    entries: list[FsEntryView]


class RecentRootView(BaseModel):
    """Недавний корень из реестра; exists=False рисуется приглушённым."""

    path: str
    exists: bool
    last_used: datetime
```

- [ ] **Step 4: Методы хаба в `hub.py`**

Импорты: `import os`, `from svarog_harness.gateway.models import FsEntryView, FsListingView, RecentRootView, SessionSummary`. В `WorkspaceHub`:

```python
    async def list_sessions(self, limit: int = 50) -> list[SessionSummary]:
        """Агрегированный список: веер по корням реестра + default, дедуп по id.

        Корни без своего db_path делят пользовательскую БД — одна сессия
        приходит из нескольких сервисов; ряды идентичны (meta в строке БД),
        так что первый занявший id выигрывает. Исчезнувший корень пропускаем:
        его сессии вернутся вместе с папкой (реестр — кэш, не истина).
        """
        seen: dict[str, SessionSummary] = {}
        candidates = [self.default_root] + [root for root, _ in self.registry.roots()]
        for root in candidates:
            try:
                svc = self.service_for(root)
            except RootPathError:
                continue
            for summary in await svc.list_sessions(limit=limit):
                seen.setdefault(summary.session_id, summary)
        ordered = sorted(seen.values(), key=lambda s: s.updated_at, reverse=True)
        return ordered[:limit]

    def list_fs(self, path: str | None) -> FsListingView:
        """Подкаталоги для пикера: только каталоги, скрытые отфильтрованы."""
        base = Path(path).expanduser() if path else Path.home()
        try:
            base = base.resolve(strict=True)
        except OSError as exc:
            raise RootPathError(f"нет такого каталога: {path}") from exc
        if not base.is_dir():
            raise RootPathError(f"не каталог: {base}")
        try:
            children = sorted(base.iterdir(), key=lambda p: p.name.lower())
        except PermissionError as exc:
            raise RootPathError(f"нет доступа: {base}") from exc
        entries: list[FsEntryView] = []
        for child in children:
            try:
                if child.name.startswith(".") or not child.is_dir():
                    continue
                accessible = os.access(child, os.R_OK | os.X_OK)
            except OSError:
                continue  # битый symlink и подобное — просто пропускаем
            entries.append(FsEntryView(name=child.name, path=str(child), accessible=accessible))
        parent = None if base == base.parent else str(base.parent)
        return FsListingView(path=str(base), parent=parent, entries=entries)

    def recent_roots(self) -> list[RecentRootView]:
        """Недавние корни для пикера; несуществующие помечены, не выброшены."""
        return [
            RecentRootView(path=str(root), exists=root.is_dir(), last_used=ts)
            for root, ts in self.registry.roots()
        ]
```

- [ ] **Step 5: Прогнать тесты**

Run: `uv run pytest tests/test_workspace_hub.py -v`
Expected: PASS (12 tests)

- [ ] **Step 6: Линт и коммит**

```bash
uv run ruff format src/svarog_harness/gateway/hub.py src/svarog_harness/gateway/models.py tests/test_workspace_hub.py
uv run ruff check src/svarog_harness/gateway/hub.py src/svarog_harness/gateway/models.py tests/test_workspace_hub.py
uv run mypy src
git add src/svarog_harness/gateway/hub.py src/svarog_harness/gateway/models.py tests/test_workspace_hub.py
git commit -m "feat(gateway): агрегированный список сессий и /fs-модели WorkspaceHub"
```

---

### Task 4: API — `path`, маршрутизация запросов, `GET /fs`

**Files:**
- Modify: `src/svarog_harness/gateway/api.py` (`_require_service` ~строка 187, `create_run` ~205, `create_session` ~305, `list_sessions` ~321, `run_events` ~504, регистрация `/fs` рядом с named workspaces ~436)
- Test: `tests/test_workspace_api.py`

**Interfaces:**
- Consumes: Task 2–3 (`WorkspaceHub`, `RootPathError`, `RootGoneError`, `FsListingView`, `RecentRootView`, `path` в запросах).
- Produces: HTTP-контракт —
  `POST /sessions {path}` / `POST /runs {path}` (422 вне single-tenant или на плохой путь);
  маршрутизация всех запросов с `{session_id}`/`{run_id}` и заголовком `X-Svarog-Root`;
  `GET /sessions` — агрегированный список; `GET /fs?path=`, `GET /fs/recent` (только с хабом);
  410 на исчезнувший корень.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_workspace_api.py
"""API-тесты выбора рабочей папки (спека 2026-07-30): path, маршрутизация, /fs."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app
from svarog_harness.gateway.hub import WorkspaceHub
from svarog_harness.gateway.roots import WorkspaceRootsRegistry


def _write_root(root: Path, db: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {db}\n",
        encoding="utf-8",
    )


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    default_root = tmp_path / "default"
    _write_root(default_root, tmp_path / "default.db")
    hub = WorkspaceHub(
        load_config(project_dir=default_root),
        default_root,
        registry=WorkspaceRootsRegistry(tmp_path / "roots.json"),
    )
    return TestClient(create_app(resolver=hub))


def test_create_session_with_path_routes_follow_ups(client: TestClient, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    created = client.post("/sessions", json={"title": "чат", "path": str(other)})
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    assert created.json()["workspace"] == str(other.resolve())
    # Follow-up маршрутизируется в сервис корня по session_id из пути URL.
    thread = client.get(f"/sessions/{session_id}/messages")
    assert thread.status_code == 200
    # Агрегированный список видит сессию чужого корня.
    listed = client.get("/sessions").json()
    assert [s["session_id"] for s in listed] == [session_id]
    assert listed[0]["workspace"] == str(other.resolve())


def test_create_session_path_errors(client: TestClient, tmp_path: Path) -> None:
    missing = client.post("/sessions", json={"title": "x", "path": str(tmp_path / "нет")})
    assert missing.status_code == 422
    both = client.post(
        "/sessions", json={"title": "x", "path": str(tmp_path), "workspace": "named"}
    )
    assert both.status_code == 422


def test_gone_root_is_410(client: TestClient, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    session_id = client.post("/sessions", json={"path": str(other)}).json()["session_id"]
    shutil.rmtree(other)
    assert client.get(f"/sessions/{session_id}/messages").status_code == 410


def test_fs_listing_and_recent(client: TestClient, tmp_path: Path) -> None:
    base = tmp_path / "обзор"
    (base / "внутри").mkdir(parents=True)
    (base / ".скрытая").mkdir()
    listing = client.get("/fs", params={"path": str(base)})
    assert listing.status_code == 200
    assert [e["name"] for e in listing.json()["entries"]] == ["внутри"]
    assert client.get("/fs", params={"path": str(base / "нет")}).status_code == 422
    other = tmp_path / "недавний"
    _write_root(other, tmp_path / "недавний.db")
    client.post("/sessions", json={"path": str(other)})
    recents = client.get("/fs/recent").json()
    assert str(other) in [r["path"] for r in recents]


def test_single_service_mode_has_no_fs_and_rejects_path(tmp_path: Path) -> None:
    """Без WorkspaceHub (multi-tenant и legacy-тесты) фичи не существует."""
    root = tmp_path / "root"
    _write_root(root, tmp_path / "root.db")
    service = GatewayService(load_config(project_dir=root), root)
    plain = TestClient(create_app(service))
    assert plain.get("/fs").status_code == 404
    assert plain.post("/sessions", json={"title": "x", "path": str(root)}).status_code == 422
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_workspace_api.py -v`
Expected: FAIL — `/fs` 404 в hub-режиме, `path` игнорируется (workspace = default), 410 не возвращается.

- [ ] **Step 3: Правки `api.py`**

Импорты: добавить `Request` в импорт из `fastapi`; из хаба —
`from svarog_harness.gateway.hub import GatewayResolver, RootGoneError, RootPathError, SingleTenantResolver, TenantHub, WorkspaceHub`;
в импорт из `gateway.models` — `FsListingView`, `RecentRootView`.

Заменить `_require_service` (строка ~187):

```python
    def _require_service(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_svarog_root: Annotated[str | None, Header()] = None,
    ) -> GatewayService:
        svc = resolver.authenticate(authorization)
        if svc is None:
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")
        if isinstance(resolver, WorkspaceHub):
            # Маршрутизация по корню: id из пути URL (какой найдётся) либо
            # явный заголовок; сессии до фичи проваливаются в default_root.
            try:
                return resolver.route(
                    session_id=request.path_params.get("session_id"),
                    run_id=request.path_params.get("run_id"),
                    root=x_svarog_root,
                )
            except RootPathError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
            except RootGoneError as exc:
                raise HTTPException(status_code=410, detail=str(exc)) from None
        return svc
```

Хелпер сразу после `ServiceDep`:

```python
    def _service_for_path(path: str | None, fallback: GatewayService) -> GatewayService:
        """Сервис корня из `path` тела create-запроса (спека 2026-07-30)."""
        if path is None:
            return fallback
        if not isinstance(resolver, WorkspaceHub):
            raise HTTPException(
                status_code=422, detail="path поддерживается только в single-tenant режиме"
            )
        try:
            return resolver.service_for(path)
        except RootPathError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
```

В `create_run` (строка ~206) первой строкой тела:

```python
        service = _service_for_path(req.path, service)
```

В `create_session` (строка ~306) — так же:

```python
        service = _service_for_path(req.path, service)
```

`list_sessions` (строка ~321):

```python
    @app.get("/sessions", response_model=list[SessionSummary])
    async def list_sessions(service: ServiceDep, limit: int = 50) -> list[SessionSummary]:
        if isinstance(resolver, WorkspaceHub):
            return await resolver.list_sessions(limit=limit)
        return await service.list_sessions(limit=limit)
```

WS `run_events` (строка ~504): после `service = resolver.authenticate(...)` добавить

```python
        if service is not None and isinstance(resolver, WorkspaceHub):
            try:
                service = resolver.route(run_id=run_id)
            except (RootPathError, RootGoneError):
                service = None  # закроется ниже как policy violation
```

Рядом с блоком named workspaces (строка ~436), регистрация только для хаба:

```python
    # --- обзор ФС для пикера рабочей папки (спека 2026-07-30) -------------
    # Только single-tenant: в multi-tenant режиме маршрутов не существует.
    if isinstance(resolver, WorkspaceHub):
        hub_resolver = resolver

        @app.get(
            "/fs", response_model=FsListingView, dependencies=[Depends(_require_service)]
        )
        async def list_fs(path: str | None = None) -> FsListingView:
            try:
                return hub_resolver.list_fs(path)
            except RootPathError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None

        @app.get(
            "/fs/recent",
            response_model=list[RecentRootView],
            dependencies=[Depends(_require_service)],
        )
        async def recent_roots() -> list[RecentRootView]:
            return hub_resolver.recent_roots()
```

- [ ] **Step 4: Прогнать новые тесты**

Run: `uv run pytest tests/test_workspace_api.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Регресс gateway-тестов**

Run: `uv run pytest tests/test_cloud_sessions.py tests/test_cloud_workspaces.py tests/test_workspace_hub.py -q`
Expected: PASS — `SingleTenantResolver`-путь не изменился.

- [ ] **Step 6: Линт и коммит**

```bash
uv run ruff format src/svarog_harness/gateway/api.py tests/test_workspace_api.py
uv run ruff check src/svarog_harness/gateway/api.py tests/test_workspace_api.py
uv run mypy src
git add src/svarog_harness/gateway/api.py tests/test_workspace_api.py
git commit -m "feat(gateway): path при создании, маршрутизация по корню и GET /fs"
```

---

### Task 5: CLI — `svarog serve` поднимает `WorkspaceHub`

**Files:**
- Modify: `src/svarog_harness/cli/main.py:1483-1485` (single-tenant ветка serve)
- Test: `tests/test_workspace_api.py` (дописать один тест)

**Interfaces:**
- Consumes: `WorkspaceHub`, `WorkspaceRootsRegistry`, `create_app(resolver=...)`.
- Produces: работающий `svarog serve` с пикером; реестр в `~/.svarog/workspace-roots.json`.

- [ ] **Step 1: Дописать падающий тест**

```python
# в конец tests/test_workspace_api.py
def test_hub_registry_survives_restart(tmp_path: Path) -> None:
    """Рестарт serve: новый хаб с тем же реестром маршрутизирует старую сессию."""
    default_root = tmp_path / "default"
    _write_root(default_root, tmp_path / "default.db")
    other = tmp_path / "other"
    _write_root(other, tmp_path / "other.db")
    registry_path = tmp_path / "roots.json"

    def make_client() -> TestClient:
        hub = WorkspaceHub(
            load_config(project_dir=default_root),
            default_root,
            registry=WorkspaceRootsRegistry(registry_path),
        )
        return TestClient(create_app(resolver=hub))

    session_id = make_client().post("/sessions", json={"path": str(other)}).json()["session_id"]
    reborn = make_client()  # «рестарт»: свежий хаб, тот же файл реестра
    thread = reborn.get(f"/sessions/{session_id}/messages")
    assert thread.status_code == 200
```

Run: `uv run pytest tests/test_workspace_api.py::test_hub_registry_survives_restart -v`
Expected: PASS сразу (механика готова в Task 4) — тест стережёт контракт рестарта; если упал — чинить Task 4, не тест.

- [ ] **Step 2: Правка `cli/main.py`**

Импорты рядом с существующим `from svarog_harness.gateway... import` в команде `serve`
(локальные импорты — по образцу соседних строк той же функции):

```python
        from svarog_harness.gateway.hub import WorkspaceHub
        from svarog_harness.gateway.roots import WorkspaceRootsRegistry
```

Заменить ветку (строки 1483-1485):

```python
    else:
        api = create_app(GatewayService(cfg, workspace), bearer_token=token)
        mode = f"single-tenant | workspace: {workspace}"
```

на:

```python
    else:
        # Single-tenant — через WorkspaceHub: рабочая папка выбирается в UI
        # при создании чата (спека 2026-07-30), корень запуска — default_root.
        registry = WorkspaceRootsRegistry(Path("~/.svarog/workspace-roots.json").expanduser())
        api = create_app(
            resolver=WorkspaceHub(cfg, workspace, registry=registry, bearer_token=token)
        )
        mode = f"single-tenant | workspace: {workspace}"
```

- [ ] **Step 3: Прогнать CLI-тесты**

Run: `uv run pytest tests/test_cli.py tests/test_workspace_api.py -q`
Expected: PASS

- [ ] **Step 4: Ручная проверка**

Run: `cd /tmp && mkdir -p svarog-manual && cd svarog-manual && uv run --project /Users/kravtandr/proj/Svarog-Agent-Harness svarog serve` (Ctrl+C после старта)
Expected: баннер прежний; `curl http://127.0.0.1:8080/fs | head -c 200` возвращает JSON с подкаталогами `$HOME`.

- [ ] **Step 5: Линт и коммит**

```bash
uv run ruff format src/svarog_harness/cli/main.py tests/test_workspace_api.py
uv run ruff check src/svarog_harness/cli/main.py tests/test_workspace_api.py
uv run mypy src
git add src/svarog_harness/cli/main.py tests/test_workspace_api.py
git commit -m "feat(cli): svarog serve поднимает WorkspaceHub в single-tenant"
```

---

### Task 6: Клиент API — типы, `fs`, `createSession(path)`, `withRoot`

**Files:**
- Modify: `web/src/api/types.ts`, `web/src/api/client.ts`, `web/src/test/fakeApi.ts`
- Test: `web/src/api/client.test.ts` (дописать)

**Interfaces:**
- Consumes: HTTP-контракт Task 4.
- Produces (для Task 7–8):
  - типы `FsEntry {name, path, accessible}`, `FsListing {path, parent, entries}`, `RecentRoot {path, exists, last_used}`;
  - `Api.createSession(title: string, path?: string)`;
  - `Api.fs(path?: string): Promise<FsListing>`; `Api.fsRecent(): Promise<RecentRoot[]>`;
  - `Api.withRoot(root: string | null): Api` — копия клиента с заголовком `X-Svarog-Root`.

- [ ] **Step 1: Дописать падающие тесты в `web/src/api/client.test.ts`**

По образцу существующих тестов файла (mock `fetch`), добавить:

```ts
describe("пикер рабочей папки", () => {
  it("createSession шлёт path только когда он задан", async () => {
    const fetchMock = mockFetch({ session_id: "s1" });
    const api = createClient({ baseUrl: "" });
    await api.createSession("Новый чат", "/home/u/proj");
    expect(JSON.parse(lastInit(fetchMock).body as string)).toEqual({
      title: "Новый чат",
      path: "/home/u/proj",
    });
    await api.createSession("Новый чат");
    expect(JSON.parse(lastInit(fetchMock).body as string)).toEqual({
      title: "Новый чат",
    });
  });

  it("fs кодирует путь, fsRecent зовёт /fs/recent", async () => {
    const fetchMock = mockFetch({ path: "/", parent: null, entries: [] });
    const api = createClient({ baseUrl: "" });
    await api.fs("/home/у же");
    expect(lastUrl(fetchMock)).toBe("/fs?path=%2Fhome%2F%D1%83%20%D0%B6%D0%B5");
    await api.fs();
    expect(lastUrl(fetchMock)).toBe("/fs");
    await api.fsRecent();
    expect(lastUrl(fetchMock)).toBe("/fs/recent");
  });

  it("withRoot добавляет X-Svarog-Root ко всем запросам копии", async () => {
    const fetchMock = mockFetch([]);
    const api = createClient({ baseUrl: "", token: "т" });
    await api.withRoot("/home/u/proj").skills();
    const headers = lastInit(fetchMock).headers as Record<string, string>;
    expect(headers["X-Svarog-Root"]).toBe("/home/u/proj");
    expect(headers.Authorization).toBe("Bearer т");
    await api.skills(); // исходный клиент — без заголовка
    expect(
      (lastInit(fetchMock).headers as Record<string, string>)["X-Svarog-Root"],
    ).toBeUndefined();
  });
});
```

Хелперы `mockFetch`/`lastInit`/`lastUrl` — использовать те, что уже есть в
`client.test.ts`; если их имена отличаются, адаптировать вызовы к местным,
не меняя проверяемые утверждения.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `npm --prefix web test -- --run src/api/client.test.ts`
Expected: FAIL — `api.fs is not a function`

- [ ] **Step 3: Типы в `types.ts`**

```ts
/** Подкаталог из GET /fs (пикер рабочей папки). */
export interface FsEntry {
  name: string;
  path: string;
  accessible: boolean;
}

export interface FsListing {
  path: string;
  parent: string | null;
  entries: FsEntry[];
}

/** Недавний корень из GET /fs/recent; exists=false — папка исчезла. */
export interface RecentRoot {
  path: string;
  exists: boolean;
  last_used: string;
}
```

- [ ] **Step 4: Клиент в `client.ts`**

`ClientOptions` получает `root?: string`; импорт типов дополнить `FsListing`, `RecentRoot`. В `request` после строки с токеном:

```ts
    if (root) headers["X-Svarog-Root"] = root;
```

(в деструктуринге параметров `createClient` добавить `root`). В интерфейс `Api`:

```ts
  createSession(title: string, path?: string): Promise<{ session_id: string }>;
  fs(path?: string): Promise<FsListing>;
  fsRecent(): Promise<RecentRoot[]>;
  /** Копия клиента с X-Svarog-Root: workspace-экраны активной сессии. */
  withRoot(root: string | null): Api;
```

Реализация: результат `createClient` присвоить константе, чтобы `withRoot` мог вернуть `api` без копии:

```ts
  const api: Api = {
    // ...существующие методы без изменений...
    createSession: (title, path) =>
      request<{ session_id: string }>("/sessions", {
        method: "POST",
        body: JSON.stringify({ title, ...(path ? { path } : {}) }),
      }),
    fs: (path) =>
      request<FsListing>(path ? `/fs?path=${encodeURIComponent(path)}` : "/fs"),
    fsRecent: () => request<RecentRoot[]>("/fs/recent"),
    withRoot: (nextRoot) =>
      nextRoot ? createClient({ baseUrl, token, root: nextRoot }) : api,
  };
  return api;
```

`deleteSession` использует свой fetch — добавить и туда `if (root) headers["X-Svarog-Root"] = root;` рядом с Authorization.

- [ ] **Step 5: `fakeApi.ts`**

Рядом с существующими моками добавить:

```ts
    fs: vi.fn().mockResolvedValue({ path: "/home/u", parent: "/home", entries: [] }),
    fsRecent: vi.fn().mockResolvedValue([]),
    withRoot: vi.fn().mockReturnThis(),
```

и в мок `createSession` ничего не менять (сигнатура шире, мок совместим).

- [ ] **Step 6: Прогнать web-тесты**

Run: `npm --prefix web test -- --run`
Expected: PASS (все, включая новые)

- [ ] **Step 7: Коммит**

```bash
git add web/src/api/types.ts web/src/api/client.ts web/src/api/client.test.ts web/src/test/fakeApi.ts
git commit -m "feat(web): клиент /fs, path в createSession и withRoot-заголовок"
```

---

### Task 7: Компонент `WorkspacePicker`

**Files:**
- Create: `web/src/components/WorkspacePicker.tsx`, `web/src/components/WorkspacePicker.css`
- Test: `web/src/components/WorkspacePicker.test.tsx`

**Interfaces:**
- Consumes: `Api.fs`, `Api.fsRecent` (Task 6), `Completion`/`CompletionItem` из `./Completion`.
- Produces: `WorkspacePicker({ api, onPick, onCancel })`, где
  `onPick: (path: string) => Promise<void>` (отклонённый promise рисуется
  инлайн-ошибкой), `onCancel: () => void`.

- [ ] **Step 1: Написать падающие тесты**

```tsx
// web/src/components/WorkspacePicker.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fakeApi } from "../test/fakeApi";
import { WorkspacePicker } from "./WorkspacePicker";

function setup() {
  const api = fakeApi({
    fsRecent: vi.fn().mockResolvedValue([
      { path: "/home/u/proj/жив", exists: true, last_used: "2026-07-30T10:00:00Z" },
      { path: "/home/u/proj/умер", exists: false, last_used: "2026-07-29T10:00:00Z" },
    ]),
    fs: vi.fn().mockResolvedValue({
      path: "/home/u",
      parent: "/home",
      entries: [
        { name: "proj", path: "/home/u/proj", accessible: true },
        { name: "закрыто", path: "/home/u/закрыто", accessible: false },
      ],
    }),
  });
  const onPick = vi.fn().mockResolvedValue(undefined);
  const onCancel = vi.fn();
  render(<WorkspacePicker api={api} onPick={onPick} onCancel={onCancel} />);
  return { api, onPick, onCancel };
}

describe("WorkspacePicker", () => {
  it("недавние: живой выбирается кликом, мёртвый заблокирован", async () => {
    const { onPick } = setup();
    const alive = await screen.findByRole("button", { name: "/home/u/proj/жив" });
    await userEvent.click(alive);
    expect(onPick).toHaveBeenCalledWith("/home/u/proj/жив");
    expect(screen.getByRole("button", { name: "/home/u/proj/умер" })).toBeDisabled();
  });

  it("обзор: клик по каталогу спускается, кнопка выбирает текущий", async () => {
    const { api, onPick } = setup();
    await userEvent.click(await screen.findByRole("button", { name: "proj" }));
    await waitFor(() => expect(api.fs).toHaveBeenCalledWith("/home/u/proj"));
    await userEvent.click(screen.getByRole("button", { name: "Выбрать эту папку" }));
    expect(onPick).toHaveBeenCalled();
  });

  it("ввод пути: подсказки по префиксу, Enter подтверждает введённое", async () => {
    const { api, onPick } = setup();
    const field = await screen.findByRole("combobox", { name: "Путь к папке" });
    await userEvent.type(field, "/home/u/pr");
    await waitFor(() => expect(api.fs).toHaveBeenCalledWith("/home/u"));
    expect(await screen.findByText("proj")).toBeInTheDocument();
    await userEvent.clear(field);
    await userEvent.type(field, "/home/u/proj{Enter}");
    expect(onPick).toHaveBeenCalledWith("/home/u/proj");
  });

  it("ошибка создания рисуется инлайн", async () => {
    const { onPick } = setup();
    onPick.mockRejectedValueOnce(new Error("не каталог или не существует: /home/u/proj/жив"));
    await userEvent.click(await screen.findByRole("button", { name: "/home/u/proj/жив" }));
    expect(
      await screen.findByText("не каталог или не существует: /home/u/proj/жив"),
    ).toBeInTheDocument();
  });

  it("кнопка отмены зовёт onCancel", async () => {
    const { onCancel } = setup();
    await userEvent.click(await screen.findByRole("button", { name: "Отмена" }));
    expect(onCancel).toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `npm --prefix web test -- --run src/components/WorkspacePicker.test.tsx`
Expected: FAIL — модуль `./WorkspacePicker` не существует

- [ ] **Step 3: Реализовать компонент**

```tsx
// web/src/components/WorkspacePicker.tsx
import { useEffect, useRef, useState } from "react";

import { type Api } from "../api/client";
import type { FsListing, RecentRoot } from "../api/types";
import { Completion, type CompletionItem } from "./Completion";
import "./WorkspacePicker.css";

/**
 * Экран выбора рабочей папки нового чата (спека 2026-07-30).
 *
 * Три механики пишут в одно состояние-«кандидат»: ввод с автодополнением,
 * недавние корни и колоночный обзор ФС. Подтверждение — onPick(path);
 * отклонённый promise (422 сервера) рисуется инлайн, экран не закрывается.
 */
export function WorkspacePicker({
  api,
  onPick,
  onCancel,
}: {
  api: Api;
  onPick: (path: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [value, setValue] = useState("");
  const [suggestions, setSuggestions] = useState<CompletionItem[]>([]);
  const [active, setActive] = useState(0);
  const [recents, setRecents] = useState<RecentRoot[]>([]);
  const [listing, setListing] = useState<FsListing | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.fsRecent().then(setRecents).catch(() => setRecents([]));
    // Обзор стартует с $HOME: сервер трактует отсутствие path как домашний каталог.
    api.fs().then(setListing).catch(() => setListing(null));
  }, [api]);

  // Автодополнение: каталог до последнего «/», фильтр по остатку-префиксу.
  const timer = useRef<number>(0);
  useEffect(() => {
    window.clearTimeout(timer.current);
    const cut = value.lastIndexOf("/");
    if (cut < 0) {
      setSuggestions([]);
      return;
    }
    timer.current = window.setTimeout(() => {
      const dir = value.slice(0, cut) || "/";
      const prefix = value.slice(cut + 1).toLowerCase();
      api
        .fs(dir)
        .then((found) => {
          setSuggestions(
            found.entries
              .filter(
                (entry) =>
                  entry.accessible &&
                  entry.name.toLowerCase().startsWith(prefix),
              )
              .slice(0, 8)
              .map((entry) => ({
                value: entry.path,
                label: entry.name,
                description: entry.path,
              })),
          );
          setActive(0);
        })
        .catch(() => setSuggestions([]));
    }, 150);
    return () => window.clearTimeout(timer.current);
  }, [api, value]);

  const confirm = (path: string) => {
    setError(null);
    onPick(path).catch((exc: unknown) => {
      setError(exc instanceof Error ? exc.message : "Не удалось создать чат.");
    });
  };

  const browseTo = (path: string) => {
    setError(null);
    api
      .fs(path)
      .then(setListing)
      .catch((exc: unknown) => {
        setError(exc instanceof Error ? exc.message : "Не удалось открыть папку.");
      });
  };

  // Хлебные крошки: /home/u → ["/", "/home", "/home/u"].
  const crumbs =
    listing === null
      ? []
      : listing.path
          .split("/")
          .filter(Boolean)
          .reduce<{ name: string; path: string }[]>(
            (acc, name) => [
              ...acc,
              { name, path: `${acc[acc.length - 1]?.path ?? ""}/${name}` },
            ],
            [],
          );

  return (
    <div className="picker">
      <h2 className="picker__title">Где работать?</h2>

      <div className="picker__field">
        <input
          role="combobox"
          aria-label="Путь к папке"
          aria-expanded={suggestions.length > 0}
          aria-controls="composer-completion-listbox"
          aria-activedescendant={
            suggestions.length > 0 ? `completion-option-${active}` : undefined
          }
          className="picker__input"
          placeholder="/путь/к/проекту"
          value={value}
          autoFocus
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown" && suggestions.length > 0) {
              event.preventDefault();
              setActive((index) => (index + 1) % suggestions.length);
            } else if (event.key === "ArrowUp" && suggestions.length > 0) {
              event.preventDefault();
              setActive(
                (index) => (index - 1 + suggestions.length) % suggestions.length,
              );
            } else if (event.key === "Enter" && value.trim() !== "") {
              event.preventDefault();
              // С открытыми подсказками Enter берёт активную, иначе — ввод.
              confirm(
                suggestions.length > 0 ? suggestions[active].value : value.trim(),
              );
            } else if (event.key === "Escape") {
              setSuggestions([]);
            }
          }}
        />
        <Completion
          items={suggestions}
          active={active}
          onPick={(picked) => {
            setValue(picked);
            setSuggestions([]);
          }}
        />
      </div>

      {error !== null && <p className="picker__error">{error}</p>}

      {recents.length > 0 && (
        <section className="picker__recents">
          <h3 className="picker__heading">Недавние</h3>
          {recents.map((recent) => (
            <button
              key={recent.path}
              type="button"
              className="picker__recent"
              disabled={!recent.exists}
              title={recent.exists ? recent.path : "Папка не существует"}
              onClick={() => confirm(recent.path)}
            >
              {recent.path}
            </button>
          ))}
        </section>
      )}

      {listing !== null && (
        <section className="picker__browser">
          <h3 className="picker__heading">Обзор</h3>
          <nav className="picker__crumbs" aria-label="Путь">
            <button type="button" onClick={() => browseTo("/")}>
              /
            </button>
            {crumbs.map((crumb) => (
              <button
                key={crumb.path}
                type="button"
                onClick={() => browseTo(crumb.path)}
              >
                {crumb.name}
              </button>
            ))}
          </nav>
          <ul className="picker__dirs">
            {listing.entries.map((entry) => (
              <li key={entry.path}>
                <button
                  type="button"
                  disabled={!entry.accessible}
                  onClick={() => browseTo(entry.path)}
                >
                  {entry.name}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <footer className="picker__actions">
        <button
          type="button"
          className="picker__confirm"
          disabled={listing === null}
          onClick={() => listing !== null && confirm(listing.path)}
        >
          Выбрать эту папку
        </button>
        <button type="button" className="picker__cancel" onClick={onCancel}>
          Отмена
        </button>
      </footer>
    </div>
  );
}
```

```css
/* web/src/components/WorkspacePicker.css — по токенам соседних компонентов */
.picker {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 560px;
  margin: 48px auto;
  padding: 0 16px;
}
.picker__title { margin: 0; }
.picker__field { position: relative; }
.picker__input { width: 100%; box-sizing: border-box; padding: 8px 10px; }
.picker__error { color: var(--error, #c0392b); margin: 0; }
.picker__heading { margin: 8px 0 4px; font-size: 0.9em; opacity: 0.7; }
.picker__recent { display: block; width: 100%; text-align: left; }
.picker__recent:disabled { opacity: 0.45; }
.picker__crumbs { display: flex; gap: 4px; flex-wrap: wrap; }
.picker__dirs { list-style: none; margin: 0; padding: 0; max-height: 40vh; overflow-y: auto; }
.picker__dirs button { display: block; width: 100%; text-align: left; }
.picker__dirs button:disabled { opacity: 0.45; }
.picker__actions { display: flex; gap: 8px; justify-content: flex-end; }
```

Сверить значения переменных/отступов с `Composer.css` и `Nav.css` и привести к местным токенам (не выдумывать новые).

- [ ] **Step 4: Прогнать тесты компонента**

Run: `npm --prefix web test -- --run src/components/WorkspacePicker.test.tsx`
Expected: PASS (5 tests)

- [ ] **Step 5: Коммит**

```bash
git add web/src/components/WorkspacePicker.tsx web/src/components/WorkspacePicker.css web/src/components/WorkspacePicker.test.tsx
git commit -m "feat(web): WorkspacePicker — недавние, обзор ФС и автодополнение пути"
```

---

### Task 8: Интеграция в App — пикер на «Новый чат», бейджи корня, scoped API

**Files:**
- Modify: `web/src/App.tsx` (`startNew` ~строка 72, рендер ~146), `web/src/components/Nav.tsx` (строка сессии ~117, хелпер `rootBase`), `web/src/components/Nav.css`
- Test: `web/src/App.test.tsx` (правка ожиданий startNew + новые), `web/src/components/Nav.test.tsx` — если файла нет, тест `rootBase` положить в `App.test.tsx`

**Interfaces:**
- Consumes: `WorkspacePicker` (Task 7), `Api.withRoot`/`createSession(title, path)` (Task 6).
- Produces: поток «＋ Новый чат → пикер → чат»; бейдж корня в навигаторе и шапке; Settings/Memory/Skills-экраны получают `api.withRoot(active.workspace)`.

- [ ] **Step 1: Обновить и дописать тесты `App.test.tsx`**

Существующие проверки `createSession.toHaveBeenCalledWith("Новый чат")`:
- тест про кнопку «Новый чат» переписать: клик открывает пикер, `createSession`
  зовётся только после выбора папки;
- тест про `ensureSession` (первое сообщение без сессии) оставить как есть —
  это быстрый путь без пикера, поведение не меняется.

```tsx
it("новый чат открывает пикер и создаёт сессию с выбранным путём", async () => {
  const api = fakeApi({
    fsRecent: vi.fn().mockResolvedValue([
      { path: "/home/u/proj", exists: true, last_used: "2026-07-30T10:00:00Z" },
    ]),
  });
  render(<App api={api} />);
  await userEvent.click(await screen.findByRole("button", { name: "＋ Новый чат" }));
  expect(api.createSession).not.toHaveBeenCalled();
  await userEvent.click(await screen.findByRole("button", { name: "/home/u/proj" }));
  await waitFor(() =>
    expect(api.createSession).toHaveBeenCalledWith("Новый чат", "/home/u/proj"),
  );
});

it("отмена пикера возвращает в чат без создания сессии", async () => {
  const api = fakeApi();
  render(<App api={api} />);
  await userEvent.click(await screen.findByRole("button", { name: "＋ Новый чат" }));
  await userEvent.click(await screen.findByRole("button", { name: "Отмена" }));
  expect(api.createSession).not.toHaveBeenCalled();
  expect(screen.queryByText("Где работать?")).not.toBeInTheDocument();
});
```

Тест бейджа (туда же или в `Nav.test.tsx`):

```tsx
it("строка сессии показывает бейдж корня", async () => {
  const api = fakeApi({
    listSessions: vi.fn().mockResolvedValue([
      {
        session_id: "s1",
        title: "чат",
        workspace: "/home/u/proj/test",
        updated_at: "2026-07-30T10:00:00Z",
        runs_count: 0,
        last_state: null,
      },
    ]),
  });
  render(<App api={api} />);
  expect(await screen.findByText("test")).toBeInTheDocument();
});
```

- [ ] **Step 2: Убедиться, что новые тесты падают**

Run: `npm --prefix web test -- --run src/App.test.tsx`
Expected: FAIL — пикера нет, `createSession` зовётся сразу.

- [ ] **Step 3: Правки `App.tsx`**

Импорт: `import { WorkspacePicker } from "./components/WorkspacePicker";`
и `rootBase` из `./components/Nav` (Step 4). Состояние и колбэки:

```tsx
  const [picking, setPicking] = useState(false);

  // «＋ Новый чат» открывает пикер папки (спека 2026-07-30); сама сессия
  // создаётся уже с выбранным path в createIn.
  const startNew = useCallback(() => {
    setPicking(true);
    setSection("chat");
  }, []);

  const createIn = useCallback(
    async (path: string) => {
      const created = await api.createSession("Новый чат", path);
      setPicking(false);
      setActiveId(created.session_id);
      await reload();
    },
    [api, reload],
  );

  /** Сессия для отправки: текущая, а если её нет — быстрая, в корне serve.
      Пикер тут не открываем: человек уже написал сообщение, не блокируем его. */
  const ensureSession = useCallback(async () => {
    if (activeId !== null) return activeId;
    const created = await api.createSession("Новый чат");
    setActiveId(created.session_id);
    await reload();
    return created.session_id;
  }, [activeId, api, reload]);
```

(старый `startNew` удалить; `onNew` в Nav и ChatScreen теперь зовёт новый
`startNew` без `void`-обёртки: `onNew={startNew}`.)

Рендер: внутри `section === "chat"` ветки — пикер вместо ChatScreen, пока
`picking`:

```tsx
      {section === "chat" &&
        (picking ? (
          <WorkspacePicker
            api={api}
            onPick={createIn}
            onCancel={() => setPicking(false)}
          />
        ) : (
          <ChatScreen ... /* существующие пропсы без изменений */ />
        ))}
```

Workspace-экраны — scoped-клиент активной сессии:

```tsx
  // Настройки/память/скиллы показывают проект активной сессии, а не корня
  // serve: withRoot добавляет X-Svarog-Root ко всем их запросам.
  const scopedApi = active?.workspace ? api.withRoot(active.workspace) : api;
```

и передать `api={scopedApi}` в `SettingsScreen`, `MemoryScreen`, `SkillsScreen`
(в `RunsScreen` и `ChatScreen` — по-прежнему `api`).

Шапка: в `bar` рядом с заголовком чата:

```tsx
          {section === "chat" && active?.workspace && (
            <span className="bar__root" title={active.workspace}>
              {rootBase(active.workspace)}
            </span>
          )}
```

- [ ] **Step 4: Бейдж в `Nav.tsx`**

Хелпер рядом с `busyLabel`:

```tsx
/** Хвост пути для бейджа корня: /home/u/proj/test → test. */
export function rootBase(workspace: string | null): string | null {
  if (!workspace) return null;
  const parts = workspace.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? null;
}
```

В строке сессии, после `nav__title` (перед блоком `busy`):

```tsx
                  {rootBase(session.workspace) !== null && (
                    <span className="nav__root" title={session.workspace ?? ""}>
                      {rootBase(session.workspace)}
                    </span>
                  )}
```

В `Nav.css` (и `.bar__root` — в css шапки, `Shell.css`):

```css
.nav__root {
  font-size: 0.75em;
  opacity: 0.6;
  margin-left: 6px;
  white-space: nowrap;
}
```

- [ ] **Step 5: Прогнать все web-тесты и сборку**

Run: `npm --prefix web test -- --run && npm --prefix web run build`
Expected: PASS + сборка без ошибок

- [ ] **Step 6: Ручная сквозная проверка**

Run: `uv run svarog serve` из корня репозитория, открыть `http://127.0.0.1:8080/`.
Expected: «＋ Новый чат» → пикер; выбор папки создаёт чат; в навигаторе бейдж
корня; несуществующий путь в поле ввода → инлайн 422-ошибка.

- [ ] **Step 7: Финальный полный прогон и коммит**

```bash
uv run pytest -q
npm --prefix web test -- --run
git add web/src/App.tsx web/src/App.test.tsx web/src/components/Nav.tsx web/src/components/Nav.css web/src/components/Shell.css
git commit -m "feat(web): пикер рабочей папки на «Новый чат» и бейджи корня"
```

---

## Что сознательно не делается (сверено со спекой)

- Multi-tenant: `/fs` и `path` не существуют (Task 4, тест `test_single_service_mode_has_no_fs_and_rejects_path`).
- Смена папки живой сессии, выселение простаивающих сервисов, лимит корней — YAGNI по спеке.
- `ensureSession` (первое сообщение без явного «Новый чат») создаёт сессию в корне serve без пикера — быстрый путь сохранён, отмечено в коде комментарием.
- 410 при отправке в сессию с удалённым корнем показывается существующей плашкой ошибок ChatScreen (текст `detail` сервера: «каталог сессии удалён: …»); отдельной кнопки «выбрать другую папку» нет — путь человека тот же «＋ Новый чат». Спека упрощена в этом месте сознательно.
- Каталоги моделей/исполнителей в композере чата (`/executors`, `/models`) остаются unscoped (default-корень) — открыто финальным ревью, отложено осознанно; при необходимости скоупить только каталожные вызовы ChatScreen, не sendMessage.
