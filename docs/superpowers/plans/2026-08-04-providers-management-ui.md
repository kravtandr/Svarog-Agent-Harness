# Управление провайдерами из UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Провайдеров можно проверить на доступность, переименовать, удалить и отредактировать из вкладки «Провайдеры»; вкладка «Исполнители» подставляет модель из каталога выбранного провайдера; UI честно сообщает про `restart_required`.

**Architecture:** Три новых эндпоинта в gateway (check/rename/delete) поверх существующих механизмов `_write_deep` (валидация эффективного конфига до записи, перечитывание без живых runs) и `fetch_models` (мимо кэша каталога). Фронтенд расширяет `ProvidersPane` и `ExecutorsPane` в `SettingsScreen.tsx`, переиспользуя `CatalogList`.

**Tech Stack:** FastAPI + Pydantic (бэкенд), React + vitest + testing-library (фронтенд), pytest.

**Spec:** `docs/superpowers/specs/2026-08-04-providers-management-ui-design.md`

## Global Constraints

- Тексты UI и docstrings — по-русски, в стиле существующих (см. соседний код).
- API-ключи не попадают в yaml никогда (ADR-0006): rename переносит `api_key_ref` как есть.
- Имя провайдера валидируется regex `[A-Za-z][\w-]{0,63}` — тем же, что в `add_provider`.
- Ошибка проверки доступности — данные ответа (200), не исключение; неизвестное имя — 404.
- Бэкенд-тесты: `uv run pytest tests/test_gateway_providers_mcp.py -v` (из корня репо).
- Фронтенд-тесты: `cd web && npx vitest run src/api/client.test.ts src/screens/SettingsScreen.test.tsx`; полный прогон — `npm test` (включает tsc и prettier).
- Комментарии объясняют «почему», не «что»; не пересказывают дифф.

---

### Task 1: Бэкенд — `POST /models/providers/{name}/check`

**Files:**
- Modify: `src/svarog_harness/gateway/models.py` (рядом с `AddProviderRequest`, ~строка 281)
- Modify: `src/svarog_harness/gateway/service.py` (рядом с `provider_models`, ~строка 485; импорт из `.models` ~строка 60)
- Modify: `src/svarog_harness/gateway/api.py` (после `add_provider`, ~строка 727; импорт `ProviderCheckView` в блок `from svarog_harness.gateway.models import`)
- Test: `tests/test_gateway_providers_mcp.py`

**Interfaces:**
- Consumes: `fetch_models(provider, api_key)`, `resolve_api_key(provider, store)`, `UnknownProviderError`, `CatalogError`, `ApiKeyError` — всё уже импортировано в service.py.
- Produces: `ProviderCheckView(ok: bool, models_count: int | None, error: str | None)`; `GatewayService.check_provider(name: str) -> ProviderCheckView` (raises `UnknownProviderError`); роут `POST /models/providers/{name}/check` → 200 всегда при известном имени, 404 при неизвестном.

- [ ] **Step 1: Написать падающий тест**

В конец `tests/test_gateway_providers_mcp.py` (перед `_noop`):

```python
def test_provider_check_reports_state_honestly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверка доступности: ок и недоступность — данные ответа, не исключение."""
    from svarog_harness.gateway.catalog import CatalogError, ModelCard

    async def fake_fetch(provider, api_key, **kw):
        return [ModelCard(id="a"), ModelCard(id="b")]

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", fake_fetch)
    resp = client.post("/models/providers/local/check")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "models_count": 2, "error": None}

    async def broken_fetch(provider, api_key, **kw):
        raise CatalogError("провайдер ответил 401")

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", broken_fetch)
    resp = client.post("/models/providers/local/check")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "401" in body["error"]

    assert client.post("/models/providers/нет-такого/check").status_code == 404


def test_provider_check_bypasses_negative_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Проверить» обязан отражать состояние сейчас, а не отрицательный кэш."""
    from svarog_harness.gateway.catalog import CatalogError, ModelCard

    async def broken_fetch(provider, api_key, **kw):
        raise CatalogError("connect timeout")

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", broken_fetch)
    # Проваленный обычный запрос каталога кладёт неудачу в кэш.
    assert client.get("/models/local").status_code == 502

    async def fake_fetch(provider, api_key, **kw):
        return [ModelCard(id="a")]

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", fake_fetch)
    resp = client.post("/models/providers/local/check")
    assert resp.json() == {"ok": True, "models_count": 1, "error": None}
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `uv run pytest tests/test_gateway_providers_mcp.py::test_provider_check_reports_state_honestly -v`
Expected: FAIL — 404 от FastAPI (роут не существует).

- [ ] **Step 3: Реализация**

`models.py`, после `AddProviderRequest`:

```python
class ProviderCheckView(BaseModel):
    """Результат проверки доступности провайдера.

    Недоступность — данные проверки, не исключение: ответ всегда 200,
    человек видит причину в error.
    """

    ok: bool
    models_count: int | None = None
    error: str | None = None
```

`service.py`, добавить `ProviderCheckView` в импорт `from svarog_harness.gateway.models import (...)`; после `provider_models`:

```python
    async def check_provider(self, name: str) -> ProviderCheckView:
        """Живая проверка `/models` — мимо кэша каталога.

        Кэш хранит и отрицательные результаты (CATALOG_NEGATIVE_TTL_SEC), а
        «Проверить» обязан отражать состояние сейчас — поэтому fetch_models
        зовётся напрямую, без чтения и записи кэша.
        """
        provider = self.cfg.models.providers.get(name)
        if provider is None:
            raise UnknownProviderError(f"провайдер '{name}' не описан в models.providers")
        try:
            api_key = resolve_api_key(provider, self._runner.host_store)
            cards = await fetch_models(provider, None if api_key == "not-needed" else api_key)
        except (CatalogError, ApiKeyError) as exc:
            return ProviderCheckView(ok=False, error=str(exc))
        return ProviderCheckView(ok=True, models_count=len(cards))
```

`api.py`: добавить `ProviderCheckView` в импорт из `svarog_harness.gateway.models`; после роута `add_provider`:

```python
    @app.post("/models/providers/{name}/check", response_model=ProviderCheckView)
    async def check_provider(name: str, service: ServiceDep) -> ProviderCheckView:
        try:
            return await service.check_provider(name)
        except UnknownProviderError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
```

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest tests/test_gateway_providers_mcp.py -v`
Expected: PASS (все, включая два новых).

- [ ] **Step 5: Commit**

```bash
git add src/svarog_harness/gateway/models.py src/svarog_harness/gateway/service.py src/svarog_harness/gateway/api.py tests/test_gateway_providers_mcp.py
git commit -m "feat(gateway): проверка доступности провайдера мимо кэша каталога"
```

---

### Task 2: Бэкенд — `POST /models/providers/{name}/rename`

**Files:**
- Modify: `src/svarog_harness/gateway/models.py` (после `ProviderCheckView`)
- Modify: `src/svarog_harness/gateway/service.py` (после `add_provider`, ~строка 1484)
- Modify: `src/svarog_harness/gateway/api.py` (после роута `check_provider`)
- Test: `tests/test_gateway_providers_mcp.py`

**Interfaces:**
- Consumes: `_write_deep(values, removes=...)` (Task-независимо, уже есть); `UnknownProviderError`.
- Produces: `RenameProviderRequest(new_name: str)`; `GatewayService.rename_provider(name: str, new_name: str) -> ConfigDiffView` (raises `UnknownProviderError` → 404, `ValueError` → 422); роут `POST /models/providers/{name}/rename`.

- [ ] **Step 1: Написать падающий тест**

```python
def test_provider_rename_moves_fields_and_default(
    client: TestClient, service: GatewayService
) -> None:
    """Rename переносит поля и default; api_key_ref остаётся валидным (ADR-0006)."""
    client.post(
        "/models/providers",
        json={
            "name": "local",
            "base_url": "https://openrouter.ai/api/v1",
            "model": "deepseek/deepseek-v4-flash",
            "api_key": "sk-or-секрет",
        },
    )
    resp = client.post("/models/providers/local/rename", json={"new_name": "openrouter"})
    assert resp.status_code == 200, resp.text
    data = yaml.safe_load(service.config_path.read_text(encoding="utf-8"))
    moved = data["models"]["providers"]["openrouter"]
    assert moved["base_url"] == "https://openrouter.ai/api/v1"
    assert moved["model"] == "deepseek/deepseek-v4-flash"
    # Секрет не перевводится: ссылка переезжает как есть.
    assert moved["api_key_ref"] == "LOCAL_API_KEY"
    assert "local" not in data["models"]["providers"]
    assert data["models"]["default"] == "openrouter"
    names = [p["name"] for p in client.get("/models").json()]
    assert names == ["openrouter"]


def test_provider_rename_rejects_bad_targets(client: TestClient) -> None:
    client.post(
        "/models/providers",
        json={"name": "groq", "base_url": "https://api.groq.com/openai/v1", "model": "ll"},
    )
    # Занятое имя, кривое имя, неизвестный источник.
    assert (
        client.post("/models/providers/local/rename", json={"new_name": "groq"}).status_code
        == 422
    )
    assert (
        client.post("/models/providers/local/rename", json={"new_name": "плохое"}).status_code
        == 422
    )
    assert (
        client.post("/models/providers/нет/rename", json={"new_name": "ok"}).status_code
        == 404
    )
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `uv run pytest tests/test_gateway_providers_mcp.py::test_provider_rename_moves_fields_and_default -v`
Expected: FAIL — 404/405 (роута нет).

- [ ] **Step 3: Реализация**

`models.py`:

```python
class RenameProviderRequest(BaseModel):
    """Новое имя записи models.providers; секрет и его ref не трогаются."""

    new_name: str = Field(min_length=1, max_length=64)
```

`service.py`, после `add_provider`:

```python
    async def rename_provider(self, name: str, new_name: str) -> ConfigDiffView:
        """Перенести запись models.providers под новое имя.

        api_key_ref переезжает как есть — секрет в SecretStore остаётся под
        прежним ref, ключ перевводить не нужно. models.default обновляется,
        если указывал на старое имя. exclude_defaults: в yaml переезжает
        только то, что человек реально задал, без шума дефолтных полей.
        """
        provider = self.cfg.models.providers.get(name)
        if provider is None:
            raise UnknownProviderError(f"провайдер '{name}' не описан в models.providers")
        if not re.fullmatch(r"[A-Za-z][\w-]{0,63}", new_name):
            raise ValueError(
                "имя провайдера — латиница/цифры/дефис/подчёркивание, начинается с буквы"
            )
        if new_name == name:
            raise ValueError("новое имя совпадает со старым")
        if new_name in self.cfg.models.providers:
            raise ValueError(f"провайдер '{new_name}' уже существует")
        dump = provider.model_dump(exclude_defaults=True)
        values: dict[str, Any] = {
            f"models.providers.{new_name}.{key}": value for key, value in dump.items()
        }
        if self.cfg.models.default == name:
            values["models.default"] = new_name
        return await self._write_deep(values, removes=[f"models.providers.{name}"])
```

`api.py`: добавить `RenameProviderRequest` в импорт; после `check_provider`:

```python
    @app.post("/models/providers/{name}/rename", response_model=ConfigDiffView)
    async def rename_provider(
        name: str, req: RenameProviderRequest, service: ServiceDep
    ) -> ConfigDiffView:
        try:
            return await service.rename_provider(name, req.new_name)
        except UnknownProviderError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
```

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest tests/test_gateway_providers_mcp.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/svarog_harness/gateway/models.py src/svarog_harness/gateway/service.py src/svarog_harness/gateway/api.py tests/test_gateway_providers_mcp.py
git commit -m "feat(gateway): переименование провайдера с переносом default и api_key_ref"
```

---

### Task 3: Бэкенд — `DELETE /models/providers/{name}`

**Files:**
- Modify: `src/svarog_harness/gateway/service.py` (после `rename_provider`)
- Modify: `src/svarog_harness/gateway/api.py` (после роута `rename_provider`)
- Test: `tests/test_gateway_providers_mcp.py`

**Interfaces:**
- Consumes: `_write_deep`, `UnknownProviderError`; образец схлопывания пустой обёртки — `remove_mcp` (service.py:1583).
- Produces: `GatewayService.remove_provider(name: str) -> ConfigDiffView` (raises `UnknownProviderError` → 404, `ValueError` для дефолтного → 422); роут `DELETE /models/providers/{name}`.

- [ ] **Step 1: Написать падающий тест**

```python
def test_provider_remove_guards_default_and_collapses_wrapper(
    client: TestClient, service: GatewayService
) -> None:
    client.post(
        "/models/providers",
        json={"name": "groq", "base_url": "https://api.groq.com/openai/v1", "model": "ll"},
    )
    # Дефолтного удалять нельзя — сначала переключить.
    assert client.delete("/models/providers/local").status_code == 422
    client.post("/executors/defaults", json={"executor": "native", "provider": "groq"})
    assert client.delete("/models/providers/local").status_code == 200
    names = [p["name"] for p in client.get("/models").json()]
    assert names == ["groq"]
    data = yaml.safe_load(service.config_path.read_text(encoding="utf-8"))
    assert "local" not in data["models"]["providers"]
    assert client.delete("/models/providers/local").status_code == 404
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `uv run pytest tests/test_gateway_providers_mcp.py::test_provider_remove_guards_default_and_collapses_wrapper -v`
Expected: FAIL — 405/404 (роута нет).

- [ ] **Step 3: Реализация**

`service.py`, после `rename_provider`:

```python
    async def remove_provider(self, name: str) -> ConfigDiffView:
        """Удалить запись models.providers; дефолтного — отказ до переключения."""
        if name not in self.cfg.models.providers:
            raise UnknownProviderError(f"провайдер '{name}' не описан в models.providers")
        if name == self.cfg.models.default:
            raise ValueError(
                "нельзя удалить провайдера по умолчанию — сначала переключите «по умолчанию»"
            )
        raw = (
            yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            if self.config_path.exists()
            else {}
        )
        models_raw = raw.get("models") or {}
        providers = models_raw.get("providers") or {}
        # Пустая обёртка (`providers:` без ключей) парсится в None и валит
        # валидацию — удаляя последний ключ проектного файла, снимаем и её.
        if name in providers and len(providers) <= 1:
            target = "models" if set(models_raw.keys()) <= {"providers"} else "models.providers"
        else:
            target = f"models.providers.{name}"
        return await self._write_deep({}, removes=[target])
```

`api.py`, после `rename_provider`:

```python
    @app.delete("/models/providers/{name}", response_model=ConfigDiffView)
    async def remove_provider(name: str, service: ServiceDep) -> ConfigDiffView:
        try:
            return await service.remove_provider(name)
        except UnknownProviderError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
```

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest tests/test_gateway_providers_mcp.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/svarog_harness/gateway/service.py src/svarog_harness/gateway/api.py tests/test_gateway_providers_mcp.py
git commit -m "feat(gateway): удаление провайдера с защитой дефолтного"
```

---

### Task 4: Фронтенд — методы API-клиента

**Files:**
- Modify: `web/src/api/types.ts` (после `ProviderCard`, ~строка 191)
- Modify: `web/src/api/client.ts` (интерфейс `Api` после `providerModels` ~строка 109; реализация после `providerModels` ~строка 254)
- Modify: `web/src/test/fakeApi.ts` (после `providerModels`, ~строка 80)
- Test: `web/src/api/client.test.ts`

**Interfaces:**
- Consumes: `request<T>(path, init)`, тип `ConfigDiff` — уже есть.
- Produces (используют Tasks 5–6): тип `ProviderCheck {ok, models_count, error}`; методы `Api.providerCheck(name): Promise<ProviderCheck>`, `Api.providerRename(name, newName): Promise<ConfigDiff>`, `Api.providerRemove(name): Promise<ConfigDiff>`.

- [ ] **Step 1: Написать падающий тест**

В `web/src/api/client.test.ts`, внутрь `describe`:

```ts
  it("экранирует имя провайдера в check/rename/delete", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const api = createClient({ baseUrl: "" });

    await api.providerCheck("a/b");
    await api.providerRename("a/b", "openrouter");
    await api.providerRemove("a/b");

    const urls = fetchMock.mock.calls.map((call) => call[0] as string);
    expect(urls).toEqual([
      "/models/providers/a%2Fb/check",
      "/models/providers/a%2Fb/rename",
      "/models/providers/a%2Fb",
    ]);
    const [, renameInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(JSON.parse(renameInit.body as string)).toEqual({
      new_name: "openrouter",
    });
    const [, deleteInit] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(deleteInit.method).toBe("DELETE");
  });
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd web && npx vitest run src/api/client.test.ts`
Expected: FAIL — `api.providerCheck is not a function` (плюс ошибка tsc, если гонять `npm test`).

- [ ] **Step 3: Реализация**

`types.ts`, после `ProviderCard`:

```ts
export interface ProviderCheck {
  ok: boolean;
  models_count: number | null;
  error: string | null;
}
```

`client.ts`: добавить `ProviderCheck` в импорт из `./types`; в интерфейс `Api` после `providerModels`:

```ts
  /** Живая проверка /models провайдера — мимо кэша каталога. */
  providerCheck(name: string): Promise<ProviderCheck>;
  providerRename(name: string, newName: string): Promise<ConfigDiff>;
  providerRemove(name: string): Promise<ConfigDiff>;
```

В реализацию `createClient` после `providerModels`:

```ts
    providerCheck: (name) =>
      request<ProviderCheck>(
        `/models/providers/${encodeURIComponent(name)}/check`,
        { method: "POST" },
      ),
    providerRename: (name, newName) =>
      request<ConfigDiff>(
        `/models/providers/${encodeURIComponent(name)}/rename`,
        { method: "POST", body: JSON.stringify({ new_name: newName }) },
      ),
    providerRemove: (name) =>
      request<ConfigDiff>(`/models/providers/${encodeURIComponent(name)}`, {
        method: "DELETE",
      }),
```

`fakeApi.ts`, после `providerModels`:

```ts
    providerCheck: vi
      .fn()
      .mockResolvedValue({ ok: true, models_count: 1, error: null }),
    providerRename: vi.fn().mockResolvedValue({
      path: "",
      lines: [],
      changes: 0,
      restart_required: false,
    }),
    providerRemove: vi.fn().mockResolvedValue({
      path: "",
      lines: [],
      changes: 0,
      restart_required: false,
    }),
```

- [ ] **Step 4: Тесты зелёные**

Run: `cd web && npx vitest run src/api/client.test.ts src/screens/SettingsScreen.test.tsx`
Expected: PASS (старые тесты не тронуты — fakeApi только пополнен).

- [ ] **Step 5: Commit**

```bash
git add web/src/api/types.ts web/src/api/client.ts web/src/test/fakeApi.ts web/src/api/client.test.ts
git commit -m "feat(web): методы клиента check/rename/delete провайдера"
```

---

### Task 5: Фронтенд — действия в списке провайдеров + заметка restart_required

**Files:**
- Modify: `web/src/screens/SettingsScreen.tsx` (компонент `ProvidersPane`, строки 202–452)
- Modify: `web/src/screens/SettingsScreen.css` (добавить `.provider__rename`)
- Test: `web/src/screens/SettingsScreen.test.tsx`

**Interfaces:**
- Consumes: `api.providerCheck / providerRename / providerRemove` (Task 4), `counted` (уже импортирован), `ConfigDiff` из `../api/types`.
- Produces: кнопки «Проверить», «Изменить», «Переименовать», «Удалить» в строке провайдера; статус «Правка записана, вступит в силу после завершения текущих запусков.» при `restart_required`.

- [ ] **Step 1: Написать падающие тесты**

В `SettingsScreen.test.tsx` после теста «разворачивает каталог…»:

```tsx
  const twoProviders = () =>
    vi.fn().mockResolvedValue([
      { name: "local", base_url: "https://openrouter.ai/api/v1", model: "deepseek/x", is_default: true },
      { name: "groq", base_url: "https://api.groq.com/openai/v1", model: "ll", is_default: false },
    ]);

  it("проверяет доступность провайдера из строки", async () => {
    const api = fakeApi({
      providers: twoProviders(),
      providerCheck: vi
        .fn()
        .mockResolvedValueOnce({ ok: true, models_count: 317, error: null })
        .mockResolvedValueOnce({ ok: false, models_count: null, error: "провайдер ответил 401" }),
    });
    render(<SettingsScreen api={api} />);
    await userEvent.click(await screen.findByRole("button", { name: "Провайдеры" }));

    const buttons = await screen.findAllByRole("button", { name: "Проверить" });
    await userEvent.click(buttons[0]);
    expect(await screen.findByText(/доступен · 317 моделей/)).toBeInTheDocument();
    expect(api.providerCheck).toHaveBeenCalledWith("local");

    await userEvent.click(buttons[1]);
    expect(await screen.findByText(/провайдер ответил 401/)).toBeInTheDocument();
  });

  it("переименовывает провайдера через инлайн-поле", async () => {
    const api = fakeApi({ providers: twoProviders() });
    render(<SettingsScreen api={api} />);
    await userEvent.click(await screen.findByRole("button", { name: "Провайдеры" }));

    const renames = await screen.findAllByRole("button", { name: "Переименовать" });
    await userEvent.click(renames[0]);
    const field = screen.getByRole("textbox", { name: "Новое имя local" });
    await userEvent.clear(field);
    await userEvent.type(field, "openrouter");
    await userEvent.click(screen.getByRole("button", { name: "OK" }));

    await waitFor(() =>
      expect(api.providerRename).toHaveBeenCalledWith("local", "openrouter"),
    );
    // Список перечитан после успеха.
    expect(api.providers).toHaveBeenCalledTimes(2);
  });

  it("удаляет провайдера после повторного клика, дефолтный — без кнопки", async () => {
    const api = fakeApi({ providers: twoProviders() });
    render(<SettingsScreen api={api} />);
    await userEvent.click(await screen.findByRole("button", { name: "Провайдеры" }));

    // «Удалить» есть только у не-дефолтного.
    const remove = await screen.findByRole("button", { name: "Удалить" });
    await userEvent.click(remove);
    expect(api.providerRemove).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Точно удалить?" }));
    await waitFor(() => expect(api.providerRemove).toHaveBeenCalledWith("groq"));
  });

  it("«Изменить» заполняет форму значениями провайдера", async () => {
    const api = fakeApi({ providers: twoProviders() });
    render(<SettingsScreen api={api} />);
    await userEvent.click(await screen.findByRole("button", { name: "Провайдеры" }));

    const edits = await screen.findAllByRole("button", { name: "Изменить" });
    await userEvent.click(edits[1]);
    expect(screen.getByLabelText("Имя")).toHaveValue("groq");
    expect(screen.getByLabelText("Base URL (с /v1)")).toHaveValue("https://api.groq.com/openai/v1");
    expect(screen.getByLabelText("Модель по умолчанию")).toHaveValue("ll");
    // Ключ не подставляется: пустое поле = не менять.
    expect(screen.getByLabelText("API-ключ (опционально)")).toHaveValue("");
  });

  it("сообщает про restart_required при сохранении провайдера", async () => {
    const api = fakeApi({
      addProvider: vi.fn().mockResolvedValue({
        path: "",
        lines: [],
        changes: 1,
        restart_required: true,
      }),
    });
    render(<SettingsScreen api={api} />);
    await userEvent.click(await screen.findByRole("button", { name: "Провайдеры" }));

    await userEvent.type(screen.getByLabelText("Имя"), "groq");
    await userEvent.type(screen.getByLabelText("Base URL (с /v1)"), "https://api.groq.com/openai/v1");
    await userEvent.type(screen.getByLabelText("Модель по умолчанию"), "ll");
    await userEvent.click(screen.getByRole("button", { name: "Сохранить провайдера" }));

    expect(
      await screen.findByText(/вступит в силу.*текущ.*запуск/i),
    ).toBeInTheDocument();
  });
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `cd web && npx vitest run src/screens/SettingsScreen.test.tsx`
Expected: FAIL — кнопок «Проверить»/«Переименовать»/«Удалить»/«Изменить» нет; restart-текста нет.

- [ ] **Step 3: Реализация в `ProvidersPane`**

Импорт: `ProviderCard` уже импортирован; добавить `ConfigDiff` в импорт из `../api/types`.

Новое состояние и обработчики (после существующих `useState`):

```tsx
  const [checks, setChecks] = useState<Record<string, string>>({});
  const [renaming, setRenaming] = useState<{ name: string; value: string } | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);

  // Один текст на все записи конфига: при живом запуске правка легла в файл,
  // но снимок не перечитан (ADR-0015) — молчать об этом значит показывать
  // список «без» только что сохранённого провайдера.
  const applied = (diff: ConfigDiff, message: string) =>
    setStatus(
      diff.restart_required
        ? "Правка записана, вступит в силу после завершения текущих запусков."
        : message,
    );

  const runCheck = async (provider: string) => {
    setChecks((current) => ({ ...current, [provider]: "проверяем…" }));
    try {
      const result = await api.providerCheck(provider);
      setChecks((current) => ({
        ...current,
        [provider]: result.ok
          ? `доступен · ${counted(result.models_count ?? 0, "модель", "модели", "моделей")}`
          : (result.error ?? "недоступен"),
      }));
    } catch (exc: unknown) {
      setChecks((current) => ({
        ...current,
        [provider]:
          exc instanceof ApiError ? exc.message : "не удалось проверить",
      }));
    }
  };

  const startEdit = (card: ProviderCard) => {
    setName(card.name);
    setBaseUrl(card.base_url);
    setModel(card.model);
    setApiKey("");
    setStatus(
      `Правьте форму ниже — «Сохранить провайдера» обновит «${card.name}». Пустой ключ не меняется.`,
    );
  };

  const submitRename = async () => {
    if (renaming === null) return;
    setStatus(null);
    try {
      const diff = await api.providerRename(renaming.name, renaming.value.trim());
      applied(diff, `«${renaming.name}» теперь называется «${renaming.value.trim()}».`);
      setRenaming(null);
      setOpenCatalog(null);
      setCatalogs({});
      reload();
    } catch (exc: unknown) {
      setStatus(
        exc instanceof ApiError ? exc.message : "Не удалось переименовать.",
      );
    }
  };

  const remove = async (provider: string) => {
    // Двухкликовое подтверждение вместо window.confirm: тестируемо и не
    // блокирует вкладку нативным диалогом.
    if (confirming !== provider) {
      setConfirming(provider);
      return;
    }
    setConfirming(null);
    setStatus(null);
    try {
      const diff = await api.providerRemove(provider);
      applied(diff, `Провайдер «${provider}» удалён.`);
      setOpenCatalog(null);
      setCatalogs({});
      reload();
    } catch (exc: unknown) {
      setStatus(exc instanceof ApiError ? exc.message : "Не удалось удалить.");
    }
  };
```

Правки существующих обработчиков:
- `submit`: `const diff = await api.addProvider({...});` и вместо `setStatus(...)` — `applied(diff, `Провайдер «${name.trim()}» сохранён.`)`.
- `makeDefault`: `const diff = await api.executorDefaults(...);` → `applied(diff, `Теперь по умолчанию — «${provider}».`)`.

JSX строки провайдера — заменить блок `<div className="secret">…</div>` внутри `providers.map`:

```tsx
          <div className="secret">
            {renaming !== null && renaming.name === card.name ? (
              <span className="provider__rename">
                <input
                  className="field__control"
                  aria-label={`Новое имя ${card.name}`}
                  value={renaming.value}
                  onChange={(event) =>
                    setRenaming({ name: card.name, value: event.target.value })
                  }
                />
                <button
                  type="button"
                  className="btn btn--small"
                  disabled={!renaming.value.trim()}
                  onClick={() => void submitRename()}
                >
                  OK
                </button>
                <button
                  type="button"
                  className="btn btn--small"
                  onClick={() => setRenaming(null)}
                >
                  Отмена
                </button>
              </span>
            ) : (
              <span>
                {card.name}
                {card.is_default ? " · по умолчанию" : ""}
              </span>
            )}
            <span className="secret__state">
              {card.model} · {card.base_url}
            </span>
            <span className="provider__actions">
              {!card.is_default && (
                <button
                  type="button"
                  className="btn btn--small"
                  onClick={() => void makeDefault(card.name)}
                >
                  По умолчанию
                </button>
              )}
              <button
                type="button"
                className="btn btn--small"
                onClick={() => void runCheck(card.name)}
              >
                Проверить
              </button>
              <button
                type="button"
                className="btn btn--small"
                onClick={() => startEdit(card)}
              >
                Изменить
              </button>
              <button
                type="button"
                className="btn btn--small"
                onClick={() => setRenaming({ name: card.name, value: card.name })}
              >
                Переименовать
              </button>
              {!card.is_default && (
                <button
                  type="button"
                  className="btn btn--small"
                  onClick={() => void remove(card.name)}
                >
                  {confirming === card.name ? "Точно удалить?" : "Удалить"}
                </button>
              )}
              <button
                type="button"
                className="btn btn--small"
                onClick={() => toggleCatalog(card.name)}
              >
                {openCatalog === card.name ? "Скрыть модели" : "Модели"}
              </button>
            </span>
          </div>
          {checks[card.name] !== undefined && (
            <p className="field__help">{checks[card.name]}</p>
          )}
```

`SettingsScreen.css` — в конец:

```css
.provider__rename {
  display: flex;
  gap: 8px;
  align-items: center;
}
```

- [ ] **Step 4: Тесты зелёные**

Run: `cd web && npx vitest run src/screens/SettingsScreen.test.tsx`
Expected: PASS — новые пять и все старые (в т.ч. «переключает провайдера по умолчанию»: текст статуса не изменился).

- [ ] **Step 5: Commit**

```bash
git add web/src/screens/SettingsScreen.tsx web/src/screens/SettingsScreen.css web/src/screens/SettingsScreen.test.tsx
git commit -m "feat(web): проверка, правка, переименование и удаление провайдера из списка"
```

---

### Task 6: Фронтенд — каталог моделей во вкладке «Исполнители»

**Files:**
- Modify: `web/src/screens/SettingsScreen.tsx` (компонент `ExecutorsPane`, строки 461–548)
- Test: `web/src/screens/SettingsScreen.test.tsx`

**Interfaces:**
- Consumes: `api.providerModels(name)` (есть), `CatalogList` (определён выше в том же файле), `ModelCard` (импортирован).
- Produces: под строкой исполнителя с выбранным провайдером — `CatalogList` его моделей; клик подставляет модель; заметка restart_required при сохранении дефолтов.

- [ ] **Step 1: Написать падающие тесты**

```tsx
  it("исполнители: выбор провайдера открывает каталог, клик подставляет модель", async () => {
    const api = fakeApi({
      providers: twoProviders(),
      providerModels: vi.fn().mockResolvedValue([
        {
          id: "llama-3.3-70b-versatile",
          name: "Llama 3.3 70B",
          context_length: 131072,
          input_usd_per_mtok: null,
          output_usd_per_mtok: null,
        },
      ]),
    });
    render(<SettingsScreen api={api} />);
    await userEvent.click(await screen.findByRole("button", { name: "Исполнители" }));

    await userEvent.selectOptions(
      await screen.findByRole("combobox", { name: "Провайдер native" }),
      "groq",
    );
    await userEvent.click(await screen.findByText("Llama 3.3 70B"));

    expect(api.providerModels).toHaveBeenCalledWith("groq");
    expect(screen.getByRole("textbox", { name: "Модель native" })).toHaveValue(
      "llama-3.3-70b-versatile",
    );
    // Поле остаётся редактируемым руками: каталог бывает неполным.
    await userEvent.clear(screen.getByRole("textbox", { name: "Модель native" }));
    await userEvent.type(
      screen.getByRole("textbox", { name: "Модель native" }),
      "своя-модель",
    );
    expect(screen.getByRole("textbox", { name: "Модель native" })).toHaveValue(
      "своя-модель",
    );
  });

  it("исполнители: недоступный каталог — текст ошибки, поле работает", async () => {
    const { ApiError } = await import("../api/client");
    const api = fakeApi({
      providers: twoProviders(),
      providerModels: vi
        .fn()
        .mockRejectedValue(new ApiError(502, "провайдер ответил 401")),
    });
    render(<SettingsScreen api={api} />);
    await userEvent.click(await screen.findByRole("button", { name: "Исполнители" }));

    await userEvent.selectOptions(
      await screen.findByRole("combobox", { name: "Провайдер opencode" }),
      "groq",
    );
    expect(await screen.findByText("провайдер ответил 401")).toBeInTheDocument();
    await userEvent.type(
      screen.getByRole("textbox", { name: "Модель opencode" }),
      "вручную",
    );
    expect(
      screen.getByRole("textbox", { name: "Модель opencode" }),
    ).toHaveValue("вручную");
  });

  it("исполнители: сообщает про restart_required при сохранении дефолтов", async () => {
    const api = fakeApi({
      providers: twoProviders(),
      executorDefaults: vi.fn().mockResolvedValue({
        path: "",
        lines: [],
        changes: 1,
        restart_required: true,
      }),
    });
    render(<SettingsScreen api={api} />);
    await userEvent.click(await screen.findByRole("button", { name: "Исполнители" }));

    await userEvent.type(
      await screen.findByRole("textbox", { name: "Модель claude-code" }),
      "opus",
    );
    const saves = screen.getAllByRole("button", { name: "Сохранить" });
    await userEvent.click(saves[saves.length - 1]);

    expect(
      await screen.findByText(/вступит в силу.*текущ.*запуск/i),
    ).toBeInTheDocument();
  });
```

Примечание: `twoProviders` объявлен в Task 5; если Task 6 выполняется первым, скопировать хелпер оттуда.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `cd web && npx vitest run src/screens/SettingsScreen.test.tsx`
Expected: FAIL — каталог не появляется, restart-текст отсутствует.

- [ ] **Step 3: Реализация в `ExecutorsPane`**

Добавить состояние каталогов и загрузку (после существующих `useState`):

```tsx
  const [catalogs, setCatalogs] = useState<Record<string, ModelCard[] | string>>({});

  const loadCatalog = (provider: string) => {
    if (provider === "" || catalogs[provider] !== undefined) return;
    api
      .providerModels(provider)
      .then((cards) =>
        setCatalogs((current) => ({ ...current, [provider]: cards })),
      )
      .catch((exc: unknown) =>
        setCatalogs((current) => ({
          ...current,
          [provider]:
            exc instanceof ApiError ? exc.message : "каталог недоступен",
        })),
      );
  };
```

`save` — учесть restart_required:

```tsx
      const diff = await api.executorDefaults({
        executor: id,
        ...(draft.provider ? { provider: draft.provider } : {}),
        ...(draft.model.trim() ? { model: draft.model.trim() } : {}),
      });
      setStatus(
        diff.restart_required
          ? "Правка записана, вступит в силу после завершения текущих запусков."
          : `Дефолты «${id}» сохранены.`,
      );
```

В `onChange` селекта провайдера — грузить каталог:

```tsx
                  onChange={(e) => {
                    patch({ provider: e.target.value });
                    loadCatalog(e.target.value);
                  }}
```

После `</div>` строки (`settings__executor-row`), внутри `div.field` исполнителя:

```tsx
            {executor.provider &&
              draft.provider !== "" &&
              (() => {
                const catalog = catalogs[draft.provider];
                if (catalog === undefined)
                  return <p className="field__help">Загружаем каталог…</p>;
                if (typeof catalog === "string")
                  return <p className="field__error">{catalog}</p>;
                return (
                  <CatalogList
                    cards={catalog}
                    onPick={(id) => patch({ model: id })}
                  />
                );
              })()}
```

Импорты `ExecutorsPane` не меняются: `ModelCard`, `ApiError`, `CatalogList` уже в области видимости файла.

- [ ] **Step 4: Тесты зелёные**

Run: `cd web && npx vitest run src/screens/SettingsScreen.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/screens/SettingsScreen.tsx web/src/screens/SettingsScreen.test.tsx
git commit -m "feat(web): каталог моделей провайдера во вкладке исполнителей"
```

---

### Task 7: Финальная проверка и сборка

**Files:**
- Без новых файлов; проверка всего изменённого.

- [ ] **Step 1: Полный бэкенд-прогон**

Run: `uv run pytest tests/ -x -q`
Expected: PASS (без регрессий за пределами провайдерных тестов).

- [ ] **Step 2: Полный фронтенд-прогон**

Run: `cd web && npm test`
Expected: PASS — tsc без ошибок, prettier чист, vitest зелёный. Если prettier ругается — `npm run format` и перезапустить.

- [ ] **Step 3: Сборка бандла**

Run: `cd web && npm run build`
Expected: сборка успешна. Напомнить в итоговом сообщении: на машине, где крутится `svarog serve`, нужно пересобрать `web/dist` — иначе фичи «не будет в UI» (известная ловушка).

- [ ] **Step 4: Финальный коммит (если остались правки формата)**

```bash
git add -A && git commit -m "chore(web): формат после прогона prettier" || true
```
