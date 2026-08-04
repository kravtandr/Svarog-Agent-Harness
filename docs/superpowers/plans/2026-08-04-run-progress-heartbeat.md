# Live-прогресс run'а (секундомер + токены) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Пока run идёт (особенно внешний executor), строка «Сварог работает…» показывает секундомер и уже потреблённые токены/стоимость с bridge-прокси.

**Architecture:** Фоновый ticker в `ExternalAgentExecutor` читает `bridge.usage` и зовёт уже существующий хук `on_progress` при изменении счётчиков; gateway подключает `on_progress` к WS-событию `{"type": "progress", ...}` (нативный loop уже зовёт хук — получает прогресс бесплатно); фронтенд держит строку статуса видимой весь run, тикает секундомер локально и подмешивает токены из `progress`-событий.

**Tech Stack:** Python 3.12 + asyncio + pytest (backend), React + TypeScript + vitest (web).

**Spec:** `docs/superpowers/specs/2026-08-04-run-progress-heartbeat-design.md`

## Global Constraints

- Работать в feature-ветке (AGENTS.md: в `main` — только рабочее состояние).
- Перед каждым коммитом бэкенда: `uv run ruff check`, `uv run ruff format`, `uv run mypy src`, `uv run pytest` по затронутым файлам — зелёное.
- Коммиты — Conventional Commits со scope модуля, как в текущей истории репозитория (описания по-русски, как в `git log`).
- Событие ровно такой формы: `{"type": "progress", "iterations": int, "tokens": int, "cost_usd": float}`.
- Ticker эмитит ТОЛЬКО при изменении счётчиков usage (история WS-реплея ограничена 2000 событий, `storage/events.py:57`).
- `web/dist` в git не коммитится (gitignored) — но `npm run build` обязан проходить.

---

### Task 0: Ветка

- [ ] **Step 1: Создать feature-ветку**

```bash
cd /Users/kravtandr/proj/Svarog-Agent-Harness
git checkout -b feature/run-progress-heartbeat
```

---

### Task 1: Ticker прогресса в ExternalAgentExecutor

**Files:**
- Modify: `src/svarog_harness/runtime/external.py` (конструктор ~строка 96–148, `_execute` ~строка 190)
- Test: `tests/test_external_executor.py`

**Interfaces:**
- Consumes: `RunBridge.usage: BridgeUsage` (поля `input_tokens`, `output_tokens`, свойство `total_tokens`), `RunBridge.cost_usd()` — уже существуют в `runtime/bridge.py`; колбэк `self._on_progress(iterations: int, tokens: int, cost_usd: float, context_ratio: float, cached: int)` — сигнатура как в существующем вызове `external.py:403`.
- Produces: новый kwarg конструктора `progress_interval_sec: float = 2.0`; приватный метод `_progress_ticker(state: _StreamState) -> None` (async). Внешний контракт исполнителя не меняется.

- [ ] **Step 1: Написать падающие тесты**

В `tests/test_external_executor.py` рядом с `test_budget_exceeded_suspends_run` (там же образец конструирования `RunBridge`):

```python
async def test_progress_ticker_emits_on_usage_change(db: AsyncSession, tmp_path: Path) -> None:
    """Ticker транслирует usage с bridge по ходу стрима — но только при изменении."""
    from svarog_harness.runtime.bridge import BridgeBudget, BridgeUsage, RunBridge, UpstreamConfig

    bridge = RunBridge(
        upstream=UpstreamConfig(base_url="http://unused", api_key=None),
        budget=BridgeBudget(max_tokens=1_000_000, max_cost_usd=100.0),
        loop=asyncio.get_running_loop(),
    )
    # Usage «уже накоплен» к первому тику: одна эмиссия (переход 0 → 120),
    # дальше счётчики не меняются — повторных эмиссий быть не должно.
    bridge.usage = BridgeUsage(input_tokens=90, output_tokens=30, requests=1)
    progress_calls: list[tuple[int, int, float, float, int]] = []
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    argv = _agent_script(tmp_path, [_INIT, _RESULT], sleep_before=0.5)
    executor = ExternalAgentExecutor(
        _ScriptAdapter(argv),
        LocalEnvironment(ws),
        TraceRecorder(db),
        workspace=ws,
        timeout_sec=30.0,
        bridge=bridge,
        on_progress=lambda *args: progress_calls.append(args),
        progress_interval_sec=0.05,
    )
    outcome = await executor.run("задача", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    # За ~0.5 с sleep_before ticker тикнул ~10 раз, но usage менялся один раз.
    # Последняя запись — существующий вызов on_progress из case "result".
    ticker_calls = [c for c in progress_calls if c[1] == 120]
    assert len(ticker_calls) == 1
    assert ticker_calls[0] == (0, 120, 0.0, 0.0, 0)


async def test_progress_ticker_silent_without_usage(db: AsyncSession, tmp_path: Path) -> None:
    """Пустой usage на bridge — ticker молчит (нет ложного «0 токенов»)."""
    from svarog_harness.runtime.bridge import BridgeBudget, RunBridge, UpstreamConfig

    bridge = RunBridge(
        upstream=UpstreamConfig(base_url="http://unused", api_key=None),
        budget=BridgeBudget(max_tokens=1_000_000, max_cost_usd=100.0),
        loop=asyncio.get_running_loop(),
    )
    progress_calls: list[tuple[int, int, float, float, int]] = []
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    argv = _agent_script(tmp_path, [_INIT, _RESULT], sleep_before=0.3)
    executor = ExternalAgentExecutor(
        _ScriptAdapter(argv),
        LocalEnvironment(ws),
        TraceRecorder(db),
        workspace=ws,
        timeout_sec=30.0,
        bridge=bridge,
        on_progress=lambda *args: progress_calls.append(args),
        progress_interval_sec=0.05,
    )
    outcome = await executor.run("задача", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    # Единственный вызов — из case "result" (tokens из result-события агента),
    # тикерных эмиссий с нулевым usage нет.
    assert all(c[1] != 0 for c in progress_calls)
```

Замечание для исполнителя: `_INIT`, `_RESULT`, `_agent_script`, `_ScriptAdapter`, фикстура `db` уже есть в этом файле; `LocalEnvironment`, `TraceRecorder`, `AutonomyMode`, `RunState`, `asyncio` уже импортированы (проверить голову файла).

- [ ] **Step 2: Убедиться, что тесты падают**

```bash
uv run pytest tests/test_external_executor.py::test_progress_ticker_emits_on_usage_change tests/test_external_executor.py::test_progress_ticker_silent_without_usage -v
```

Ожидание: FAIL — `TypeError: ... unexpected keyword argument 'progress_interval_sec'`.

- [ ] **Step 3: Реализация ticker'а**

В `src/svarog_harness/runtime/external.py`:

1. В голову файла добавить `import logging` и `logger = logging.getLogger(__name__)` (логгера в модуле сейчас нет).
2. В конструктор `ExternalAgentExecutor` добавить kwarg (после `extra_run_meta`) и поле:

```python
        progress_interval_sec: float = 2.0,
```

```python
        # Период трансляции usage с bridge в on_progress (UX «не зависло»).
        self._progress_interval_sec = progress_interval_sec
```

3. Новый метод (рядом с `_stream_with_suspend`):

```python
    async def _progress_ticker(self, state: _StreamState) -> None:
        """Транслирует usage с bridge в on_progress, пока идёт стрим агента.

        Эмиссия только при изменении счётчиков: история событий для
        WS-реплея конечна, а безусловный тик за долгий run вытеснил бы из
        неё настоящие события. Число эмиссий ограничено числом LLM-запросов.
        """
        assert self._bridge is not None and self._on_progress is not None
        last = (0, 0.0)
        while True:
            await asyncio.sleep(self._progress_interval_sec)
            try:
                current = (self._bridge.usage.total_tokens, self._bridge.cost_usd())
                if current == last:
                    continue
                last = current
                self._on_progress(state.tool_calls, current[0], current[1], 0.0, 0)
            except Exception as exc:  # noqa: BLE001 — прогресс не роняет run
                logger.warning("progress-ticker: %s", exc)
```

4. В `_execute` обернуть стрим (от первого `_stream_with_suspend` до конца recovery-цикла `while`) в ticker:

```python
        ticker: asyncio.Task[None] | None = None
        if self._bridge is not None and self._on_progress is not None:
            ticker = asyncio.create_task(self._progress_ticker(state))
        try:
            result, gate_suspended = await self._stream_with_suspend(command, on_line)

            recovery_attempts = 0
            while (
                ...  # существующий recovery-цикл без изменений
            ):
                ...
        finally:
            if ticker is not None:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker
```

(Существующие строки 207–233 сдвигаются внутрь `try`; всё после `finally` — без изменений. Отмена до финализации usage гарантирует: `progress` после `run_finished` невозможен.)

- [ ] **Step 4: Прогнать тесты**

```bash
uv run pytest tests/test_external_executor.py -v
```

Ожидание: PASS все (новые два + существующие не сломаны).

- [ ] **Step 5: Линт, типы, коммит**

```bash
uv run ruff check src/svarog_harness/runtime/external.py tests/test_external_executor.py && uv run ruff format src/svarog_harness/runtime/external.py tests/test_external_executor.py && uv run mypy src
git add src/svarog_harness/runtime/external.py tests/test_external_executor.py
git commit -m "feat(runtime): ticker прогресса usage с bridge у внешнего executor"
```

---

### Task 2: Gateway публикует progress-событие

**Files:**
- Modify: `src/svarog_harness/gateway/service.py` (метод `_event_hooks`, ~строка 723 — конструктор `RunHooks`)
- Test: `tests/test_gateway_web.py` (рядом с `test_tool_events_carry_arg_and_result`, ~строка 101)

**Interfaces:**
- Consumes: `RunHooks.on_progress: Callable[[int, int, float, float, int], None] | None` (`runtime/run_assembly.py:106`) — аргументы `(iterations, tokens, cost_usd, context_ratio, cached_tokens)`; `emit(event: dict)` — локальный хелпер внутри `_event_hooks`.
- Produces: WS-событие `{"type": "progress", "iterations": int, "tokens": int, "cost_usd": float}` в `service.events` (то же, что увидит фронтенд).

- [ ] **Step 1: Написать падающий тест**

В `tests/test_gateway_web.py` после `test_tool_events_carry_arg_and_result` (тот же паттерн — подмена `events.publish`, `_RunHolder` и `_event_hooks` уже импортированы в этом файле):

```python
@pytest.mark.asyncio
async def test_progress_event_published(service: GatewayService) -> None:
    """on_progress → WS-событие progress: живой счётчик токенов в чате."""
    published: list[dict[str, object]] = []
    service.events.publish = lambda run_id, event: published.append(event)  # type: ignore[method-assign]

    started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    holder = _RunHolder()
    holder.run_id = "run-1"
    hooks = service._event_hooks(holder, started)

    assert hooks.on_progress is not None
    hooks.on_progress(3, 12_400, 0.04, 0.0, 0)

    assert published == [
        {"type": "progress", "iterations": 3, "tokens": 12_400, "cost_usd": 0.04}
    ]
```

- [ ] **Step 2: Убедиться, что тест падает**

```bash
uv run pytest tests/test_gateway_web.py::test_progress_event_published -v
```

Ожидание: FAIL — `assert hooks.on_progress is not None` (хук не подключён).

- [ ] **Step 3: Подключить хук**

В `src/svarog_harness/gateway/service.py`, в возвращаемом `RunHooks` внутри `_event_hooks` (после `on_tool_result=...`):

```python
            # Живой прогресс (токены/стоимость): у внешнего executor'а — с
            # ticker'а bridge-прокси, у нативного loop — после каждой итерации.
            on_progress=lambda iterations, tokens, cost, _ctx, _cached: emit(
                {
                    "type": "progress",
                    "iterations": iterations,
                    "tokens": tokens,
                    "cost_usd": cost,
                }
            ),
```

- [ ] **Step 4: Прогнать тесты**

```bash
uv run pytest tests/test_gateway_web.py -v
```

Ожидание: PASS.

- [ ] **Step 5: Линт, типы, коммит**

```bash
uv run ruff check src/svarog_harness/gateway/service.py tests/test_gateway_web.py && uv run ruff format src/svarog_harness/gateway/service.py tests/test_gateway_web.py && uv run mypy src
git add src/svarog_harness/gateway/service.py tests/test_gateway_web.py
git commit -m "feat(gateway): публикация progress-события в WS-стрим run'а"
```

---

### Task 3: Модель прогресса на фронтенде (progress.ts + StreamEvent)

**Files:**
- Create: `web/src/model/progress.ts`
- Create: `web/src/model/progress.test.ts`
- Modify: `web/src/model/thread.ts:27-37` (union `StreamEvent`)
- Test (дополнить): `web/src/model/thread.test.ts`

**Interfaces:**
- Produces (использует Task 4):
  - тип `RunProgress = { tokens: number; costUsd: number }`;
  - `formatElapsed(totalSec: number): string` — `83 → "1:23"`;
  - `progressLabel(elapsedSec: number, progress: RunProgress | null): string` — полная строка статуса;
  - вариант `{ type: "progress"; iterations: number; tokens: number; cost_usd: number }` в `StreamEvent`.

- [ ] **Step 1: Написать падающие тесты**

`web/src/model/progress.test.ts`:

```typescript
import { describe, expect, it } from "vitest";

import { formatElapsed, progressLabel } from "./progress";

describe("formatElapsed", () => {
  it("формат м:сс", () => {
    expect(formatElapsed(0)).toBe("0:00");
    expect(formatElapsed(7)).toBe("0:07");
    expect(formatElapsed(83)).toBe("1:23");
    expect(formatElapsed(3671)).toBe("61:11");
  });
});

describe("progressLabel", () => {
  it("без прогресса — только секундомер", () => {
    expect(progressLabel(7, null)).toBe("Сварог работает… 0:07");
  });

  it("токены с разделителем тысяч", () => {
    expect(progressLabel(83, { tokens: 12400, costUsd: 0 })).toBe(
      "Сварог работает… 1:23 · 12 400 токенов",
    );
  });

  it("стоимость видна, когда доросла до цента", () => {
    expect(progressLabel(83, { tokens: 12400, costUsd: 0.04 })).toBe(
      "Сварог работает… 1:23 · 12 400 токенов · $0.04",
    );
  });

  it("нулевые токены не показываются (bridge ещё пуст)", () => {
    expect(progressLabel(5, { tokens: 0, costUsd: 0 })).toBe(
      "Сварог работает… 0:05",
    );
  });
});
```

Дополнить `web/src/model/thread.test.ts` (в конец существующего describe про applyEvent, стиль — по соседним тестам):

```typescript
  it("progress-событие ленту не меняет", () => {
    const items = applyEvent([], {
      type: "progress",
      iterations: 3,
      tokens: 12400,
      cost_usd: 0.04,
    });
    expect(items).toEqual([]);
  });
```

- [ ] **Step 2: Убедиться, что тесты падают**

```bash
cd web && npx vitest run src/model/progress.test.ts src/model/thread.test.ts
```

Ожидание: progress.test.ts — FAIL (модуля нет); thread.test.ts — может пройти сразу (у `StreamEvent` есть catch-all `{ type: string }`, `applyEvent` неизвестные типы игнорирует, `thread.ts:215`) — это нормально, тест фиксирует контракт.

- [ ] **Step 3: Реализация**

`web/src/model/progress.ts`:

```typescript
/** Живой прогресс run'а из WS-события `progress` (см. gateway/service.py). */
export type RunProgress = { tokens: number; costUsd: number };

/** 83 → "1:23" — секундомер в строке статуса. */
export function formatElapsed(totalSec: number): string {
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return `${min}:${String(sec).padStart(2, "0")}`;
}

/** 12400 → "12 400" — вручную, а не toLocaleString: у локалей неразрывные
 * пробелы разных видов, а строка сравнивается в тестах. */
function formatTokens(tokens: number): string {
  return String(tokens).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

/**
 * Строка статуса под лентой. Токены появляются после первой эмиссии с
 * bridge (нулевой usage не показываем — «0 токенов» читается как поломка),
 * стоимость — когда доросла до отображаемого цента.
 */
export function progressLabel(
  elapsedSec: number,
  progress: RunProgress | null,
): string {
  let label = `Сварог работает… ${formatElapsed(elapsedSec)}`;
  if (progress !== null && progress.tokens > 0) {
    label += ` · ${formatTokens(progress.tokens)} токенов`;
    if (progress.costUsd >= 0.005) label += ` · $${progress.costUsd.toFixed(2)}`;
  }
  return label;
}
```

В `web/src/model/thread.ts` в union `StreamEvent` (перед catch-all вариантом `{ type: string; ... }`):

```typescript
  | { type: "progress"; iterations: number; tokens: number; cost_usd: number }
```

- [ ] **Step 4: Прогнать тесты**

```bash
cd web && npx vitest run src/model/
```

Ожидание: PASS.

- [ ] **Step 5: Коммит**

```bash
git add web/src/model/progress.ts web/src/model/progress.test.ts web/src/model/thread.ts web/src/model/thread.test.ts
git commit -m "feat(web): модель live-прогресса run'а и событие progress"
```

---

### Task 4: ChatScreen — секундомер и токены в строке статуса

**Files:**
- Modify: `web/src/screens/ChatScreen.tsx` (состояния ~строки 175–184, `watch` ~289–301, эффект смены сессии ~303–360, `send` ~460–540, рендер ~729–733)
- Test: `web/src/screens/ChatScreen.test.tsx`

**Interfaces:**
- Consumes: `progressLabel`, `RunProgress` из `../model/progress` (Task 3); события `progress` и `run_finished` из WS.
- Produces: UI-поведение; экспортов нет.

Суть: состояние `thinking` («до первого события») удаляется — строка статуса живёт всё время `running` и содержит `progressLabel(elapsed, progress)`.

- [ ] **Step 1: Написать падающие тесты**

В `web/src/screens/ChatScreen.test.tsx`, в describe «подписка на поток» (FakeSocket-паттерн скопировать из соседнего теста «переподписывается на живой run сессии», ~строка 251):

```typescript
  it("строка статуса живёт весь run и показывает прогресс", async () => {
    class FakeSocket {
      static last: FakeSocket | null = null;
      onmessage: ((e: MessageEvent<string>) => void) | null = null;
      constructor(public url: string | URL) {
        FakeSocket.last = this;
      }
      close() {}
    }
    vi.stubGlobal("WebSocket", FakeSocket);

    const client = api();
    render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );
    await waitFor(() =>
      expect(screen.getByText("Добавь FTS-поиск")).toBeInTheDocument(),
    );
    await userEvent.type(
      screen.getByRole("combobox", { name: /написать/i }),
      "долгая задача",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(client.sendMessage).toHaveBeenCalled());

    // До каких-либо событий — секундомер уже виден.
    expect(screen.getByText(/Сварог работает… \d+:\d\d/)).toBeInTheDocument();

    // Первое текстовое событие строку НЕ гасит (раньше гасило).
    act(() =>
      FakeSocket.last?.onmessage?.({
        data: JSON.stringify({ type: "text", delta: "смотрю код" }),
      } as MessageEvent<string>),
    );
    expect(screen.getByText(/Сварог работает…/)).toBeInTheDocument();

    // progress подмешивает токены и стоимость, ленту не трогает.
    act(() =>
      FakeSocket.last?.onmessage?.({
        data: JSON.stringify({
          type: "progress",
          iterations: 3,
          tokens: 12400,
          cost_usd: 0.04,
        }),
      } as MessageEvent<string>),
    );
    expect(
      screen.getByText(/Сварог работает… \d+:\d\d · 12 400 токенов · \$0\.04/),
    ).toBeInTheDocument();

    // Финал гасит строку.
    act(() =>
      FakeSocket.last?.onmessage?.({
        data: JSON.stringify({ type: "run_finished", state: "completed" }),
      } as MessageEvent<string>),
    );
    expect(screen.queryByText(/Сварог работает…/)).not.toBeInTheDocument();
    vi.unstubAllGlobals();
  });
```

- [ ] **Step 2: Убедиться, что тест падает**

```bash
cd web && npx vitest run src/screens/ChatScreen.test.tsx
```

Ожидание: FAIL на шаге «первое текстовое событие строку НЕ гасит» (текущий код гасит по первому событию).

- [ ] **Step 3: Реализация**

В `web/src/screens/ChatScreen.tsx`:

1. Импорт: `import { progressLabel, type RunProgress } from "../model/progress";`
2. Заменить состояние `thinking` (строки ~177–180, вместе с комментарием) на:

```typescript
  // Живой прогресс run'а: elapsed тикает локально (тикающее время — чисто
  // презентационное состояние, WS-события для него — шум), токены/стоимость
  // приходят событиями progress с bridge-прокси.
  const [progress, setProgress] = useState<RunProgress | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);
```

3. Эффект секундомера (после эффекта автоскролла):

```typescript
  useEffect(() => {
    if (!running || startedAt === null) {
      setElapsed(0);
      return;
    }
    const tick = () =>
      setElapsed(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [running, startedAt]);
```

4. `watch` (строки ~289–301): убрать `setThinking(false)` и комментарий про «первое событие гасит», добавить перехват `progress` ДО `applyEvent`:

```typescript
      unsubscribe.current = subscribeRun(baseUrl, runId, token, (event) => {
        if (event.type === "progress") {
          // Прогресс — отдельное состояние строки статуса, в ленту не идёт.
          const { tokens, cost_usd } = event as {
            tokens: number;
            cost_usd: number;
          };
          setProgress({ tokens, costUsd: cost_usd });
          return;
        }
        if (event.type === "run_finished") setRunning(false);
        setItems((current) => applyEvent(current, event));
      });
```

5. Эффект смены сессии (~строка 312): `setThinking(false)` заменить на `setProgress(null); setStartedAt(null);`
6. Подхват живого run'а (~строка 352): `setThinking(true)` заменить на `setProgress(null); setStartedAt(Date.now());`
7. `send` (~строка 475): `setThinking(true)` заменить на `setProgress(null); setStartedAt(Date.now());`
8. Обработчик ошибки отправки (~строка 529): `setThinking(false)` удалить (строку гасит `setRunning(false)` строкой ниже).
9. Рендер (~строки 729–733):

```tsx
          {running && (
            <p className="chat__hint chat__thinking" role="status">
              {progressLabel(elapsed, progress)}
            </p>
          )}
```

Проверить, что других чтений `thinking` не осталось (`grep -n thinking web/src/screens/ChatScreen.tsx`) — иначе tsc укажет.

- [ ] **Step 4: Прогнать тесты и сборку**

```bash
cd web && npm test && npm run build
```

Ожидание: vitest PASS (все экраны), tsc и prettier без ошибок, сборка проходит.

- [ ] **Step 5: Коммит**

```bash
git add web/src/screens/ChatScreen.tsx web/src/screens/ChatScreen.test.tsx
git commit -m "feat(web): секундомер и живые токены в строке «Сварог работает»"
```

---

### Task 5: Финальная проверка и слияние

**Files:** без новых изменений (прогоны + merge).

- [ ] **Step 1: Полные прогоны**

```bash
uv run ruff check && uv run mypy src && uv run pytest
```

```bash
cd web && npm test && npm run build
```

Ожидание: всё зелёное.

- [ ] **Step 2: Слияние по процессу finishing-a-development-branch**

Использовать skill superpowers:finishing-a-development-branch (merge в `main`, удаление ветки).

- [ ] **Step 3: Напоминание про деплой**

`web/dist` не в git: на машине, где крутится `svarog serve`, после обновления кода обязателен `npm run build` в `web/` — иначе фича «не видна в UI» (известная ловушка).
