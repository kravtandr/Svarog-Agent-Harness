# Блок B: автопродолжение долгих run'ов — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Долгая задача доводится до конца без ручного `svarog resume`, оставаясь ограниченной явным потолком раундов и бюджетами.

**Architecture:** Механика сброса контекста уже есть (`_rebuild_after_refuel`); добавляется её вызов внутри цикла вместо приостановки, счётчик раундов в состоянии run'а и потолок в конфиге. `max_iterations` при этом становится лимитом сегмента между refuel'ами, а роль общего стоп-крана переходит к потолку раундов и бюджетам токенов и стоимости.

**Tech Stack:** Python 3.12+, uv, Pydantic v2, SQLAlchemy async/SQLite, pytest (`asyncio_mode=auto`), ruff, mypy.

**Спек:** `docs/superpowers/specs/2026-07-21-refuel-autocontinue-design.md`

## Global Constraints

- Комментарии, docstring'и, сообщения об ошибках и вывод CLI — на русском; код и идентификаторы — на английском.
- Conventional Commits, заголовок ≤72 символов, на английском в императиве: `feat|fix|docs|refactor|test|chore(scope): описание`. Scope — модуль (`runtime`, `config`, `docs`).
- Перед каждым коммитом: `uv run ruff check`, `uv run ruff format`, `uv run mypy`, `uv run pytest` — всё зелёное.
- Известное пред-существующее падение, не связанное с блоком: `tests/test_external_docker.py::test_external_run_once_in_docker` (загрязнение docker-окружения). Не чинить, отмечать в отчёте.
- Набор тестов флейкает: полный прогон может дать одно падение в случайном тесте, в изоляции проходящем. Воспроизводится и до блока B. Упавший тест перезапустить изолированно; если проходит — отметить в отчёте и продолжать.
- Правила зависимостей (`docs/repo-structure.md`): `cli` → `runtime` → компоненты → `storage`/`trace`. Импортов из `cli` в ядро нет.
- Никакого мёртвого кода: неиспользуемые функции, закомментированные блоки — не проходят review.
- Секретов в коде и тестах нет.
- Порядок операций refuel не меняется: `task_state.md` записывается и коммитится ДО сброса истории.
- Старые checkpoint'ы без новых полей должны читаться (значение по умолчанию `0`).

---

## File Structure

**Модифицируется:**
- `src/svarog_harness/config/schema.py` — поле `RuntimeConfig.max_refuel_rounds`.
- `src/svarog_harness/runtime/checkpoint.py` — поле `LoopState.refuel_rounds` и его сериализация.
- `src/svarog_harness/runtime/loop.py` — условие цикла на счётчик сегмента; выделение записи `task_state.md` из `_refuel_suspend`; автопродолжение; обнуление счётчика в `resume`.
- `tests/test_refuel.py`, `tests/test_loop.py`, `tests/test_resume.py`, `tests/test_config.py` — тесты.
- `docs/adr/0005-resumable-runs.md`, `TASK.md`, `docs/reference-analysis.md` — документация.

Новых модулей не создаётся: вся логика — внутри существующего цикла, и выносить её в отдельный файл значило бы разорвать связный участок управления.

---

## Task 1: Потолок раундов в конфиге и счётчик в состоянии

**Files:**
- Modify: `src/svarog_harness/config/schema.py` (`RuntimeConfig`, около строки 103)
- Modify: `src/svarog_harness/runtime/checkpoint.py` (`LoopState`, `to_dict`, `from_dict`)
- Test: `tests/test_config.py`, `tests/test_refuel.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `RuntimeConfig.max_refuel_rounds: int` (default `12`, `ge=0`); `LoopState.refuel_rounds: int` (default `0`), сериализуемое в `to_dict`/`from_dict` под ключом `"refuel_rounds"`.

- [ ] **Step 1: Написать падающий тест**

В конец `tests/test_refuel.py` добавить:

```python
def test_loop_state_roundtrip_keeps_refuel_rounds(tmp_path: Path) -> None:
    """Счётчик раундов переживает checkpoint: иначе падение процесса
    обнуляло бы потолок само собой (блок B §4)."""
    from svarog_harness.runtime.checkpoint import LoopState

    state = LoopState(workspace=tmp_path, messages=[], task="t", refuel_rounds=3)
    restored = LoopState.from_dict(state.to_dict())
    assert restored.refuel_rounds == 3


def test_loop_state_reads_old_checkpoint_without_refuel_rounds(tmp_path: Path) -> None:
    """Checkpoint, записанный до блока B, читается со счётчиком 0."""
    from svarog_harness.runtime.checkpoint import LoopState

    state = LoopState(workspace=tmp_path, messages=[], task="t")
    raw = state.to_dict()
    del raw["refuel_rounds"]
    assert LoopState.from_dict(raw).refuel_rounds == 0


def test_runtime_config_default_refuel_rounds() -> None:
    """По умолчанию автопродолжение включено с потолком 12."""
    from svarog_harness.config.schema import RuntimeConfig

    assert RuntimeConfig().max_refuel_rounds == 12


def test_runtime_config_allows_disabling_autocontinue() -> None:
    """Ноль — прежнее поведение: приостановка на первом refuel."""
    from svarog_harness.config.schema import RuntimeConfig

    assert RuntimeConfig(max_refuel_rounds=0).max_refuel_rounds == 0
```

Реализующему: если `tests/test_refuel.py` не импортирует `Path`, добавь импорт на уровне модуля, а не внутрь тестов.

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_refuel.py -k "refuel_rounds or autocontinue" -v`
Expected: FAIL — `TypeError: LoopState.__init__() got an unexpected keyword argument 'refuel_rounds'`

- [ ] **Step 3: Добавить поле в конфиг**

В `src/svarog_harness/config/schema.py`, в `RuntimeConfig`, после поля `stagnation_repeats` добавить:

```python
    # Блок B: сколько раз подряд run продолжает себя сам после сброса контекста
    # в task_state.md. 0 — прежнее поведение (приостановка, продолжение только
    # через `svarog resume`). Потолок — защита от бесконечного цикла; реальным
    # регулятором длительности служат бюджеты токенов и стоимости.
    max_refuel_rounds: int = Field(default=12, ge=0)
```

- [ ] **Step 4: Добавить поле в состояние и сериализацию**

В `src/svarog_harness/runtime/checkpoint.py`, в `LoopState`, рядом с `iterations_since_refuel` добавить:

```python
    # Сколько автоматических продолжений после refuel уже израсходовано
    # (блок B §4). Переживает checkpoint: иначе падение процесса обнуляло бы
    # потолок. Ручной resume обнуляет счётчик — см. AgentLoop.resume.
    refuel_rounds: int = 0
```

В `to_dict` добавить рядом с `"iterations_since_refuel"`:

```python
            "refuel_rounds": self.refuel_rounds,
```

В `from_dict` добавить рядом с `iterations_since_refuel=`:

```python
            refuel_rounds=raw.get("refuel_rounds", 0),
```

- [ ] **Step 5: Запустить тесты**

Run: `uv run pytest tests/test_refuel.py tests/test_config.py -v`
Expected: PASS

- [ ] **Step 6: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/config/schema.py src/svarog_harness/runtime/checkpoint.py tests/test_refuel.py
git commit -m "feat(config): add refuel round cap and state counter"
```

---

## Task 2: `max_iterations` как лимит сегмента

**Files:**
- Modify: `src/svarog_harness/runtime/loop.py` (условие цикла `while` в `_drive`, около строки 289; сообщение о лимите, около строки 414)
- Test: `tests/test_loop.py`

**Interfaces:**
- Consumes: `LoopState.iterations_since_refuel` (существует).
- Produces: ничего нового для последующих задач.

- [ ] **Step 1: Написать падающий тест**

В `tests/test_loop.py` добавить:

```python
async def test_max_iterations_limits_segment_not_run(db: AsyncSession, tmp_path: Path) -> None:
    """Блок B §2: max_iterations ограничивает сегмент между refuel'ами.

    Сегмент = 2 итерации, потолок раундов = 2, лимит сегмента = 3. Run
    проходит 5 итераций — больше max_iterations — и завершается сам.
    """
    (tmp_path / "a.txt").write_text("содержимое", encoding="utf-8")
    call = ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "a.txt"}')
    provider = ScriptedProvider(
        [_tool_turn(call), _tool_turn(call), _tool_turn(call), _tool_turn(call), _final("готово")]
    )
    cfg = RuntimeConfig(max_iterations=3, refuel_after_iterations=2, max_refuel_rounds=2)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("работай", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    assert outcome.iterations == 5
```

Реализующему: тест зелёный станет только после Task 3 (автопродолжения ещё нет). На этом шаге он обязан падать по причине «run приостановлен», а не по синтаксису — это и есть подтверждение, что тест меряет нужное. Оставь его падающим до Task 3 и не коммить до этого момента; коммит Task 2 делается вместе с Task 3.

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_loop.py::test_max_iterations_limits_segment_not_run -v`
Expected: FAIL — `assert <RunState.SUSPENDED> is <RunState.COMPLETED>`

- [ ] **Step 3: Переключить условие цикла на счётчик сегмента**

В `src/svarog_harness/runtime/loop.py` в `_drive` заменить условие цикла:

```python
            while state.iterations < self._cfg.max_iterations:
```

на:

```python
            # Блок B §2: max_iterations ограничивает СЕГМЕНТ между refuel'ами.
            # Общие стоп-краны — потолок раундов (§1) и бюджеты токенов и
            # стоимости; state.iterations остаётся тотальным счётчиком для
            # отчётности и trace.
            while state.iterations_since_refuel < self._cfg.max_iterations:
```

И сообщение при выходе из цикла (сейчас «достигнут лимит итераций»):

```python
            return await self._suspend(
                run,
                state,
                f"достигнут лимит итераций сегмента ({self._cfg.max_iterations}); "
                f"увеличьте runtime.max_iterations и выполните resume",
            )
```

- [ ] **Step 4: Убедиться, что существующие тесты лимита итераций не сломались**

Run: `uv run pytest tests/test_loop.py tests/test_refuel.py -v`
Expected: тесты, проверявшие остановку по `max_iterations`, должны продолжать проходить: при недостижимом пороге refuel (`refuel_after_iterations > max_iterations`) сегмент совпадает со всем run'ом, и поведение прежнее. Если тест опирался на точный текст сообщения — поправь ожидание на новую формулировку, не откатывая изменение. Тест `test_max_iterations_limits_segment_not_run` на этом шаге всё ещё падает — это ожидаемо, он закрывается Task 3.

---

## Task 3: Автопродолжение в цикле

**Files:**
- Modify: `src/svarog_harness/runtime/loop.py` (`_refuel_suspend` около строки 583; ветка refuel в `_drive` около строки 410)
- Test: `tests/test_loop.py`, `tests/test_refuel.py`

**Interfaces:**
- Consumes: `RuntimeConfig.max_refuel_rounds`, `LoopState.refuel_rounds` (Task 1); `_rebuild_after_refuel` (существует).
- Produces: метод `AgentLoop._write_task_state(run, state) -> None` — запись `task_state.md` и коммит Flow C; ключ `Run.meta["refuel_rounds"]`.

- [ ] **Step 1: Написать падающие тесты**

В `tests/test_loop.py` добавить:

```python
async def test_autocontinue_finishes_task_without_manual_resume(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Блок B §3: run сам продолжает работу после сброса контекста."""
    (tmp_path / "a.txt").write_text("содержимое", encoding="utf-8")
    call = ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "a.txt"}')
    provider = ScriptedProvider(
        [_tool_turn(call), _tool_turn(call), _tool_turn(call), _tool_turn(call), _final("готово")]
    )
    cfg = RuntimeConfig(max_iterations=10, refuel_after_iterations=2, max_refuel_rounds=2)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("работай", AutonomyMode.YOLO)

    assert outcome.state is RunState.COMPLETED
    assert outcome.final_answer == "готово"

    run = await db.get(Run, outcome.run_id)
    assert run is not None
    assert run.meta["refuel_rounds"] == 2

    # Контекст действительно пересобирался: после сброса первым идёт system,
    # вторым — user с сохранённым состоянием задачи.
    third_request = provider.seen_messages[2]
    assert third_request[0].role == "system"
    assert "task_state" in third_request[1].content or "Task state" in third_request[1].content


async def test_autocontinue_stops_at_round_cap(db: AsyncSession, tmp_path: Path) -> None:
    """Потолок раундов исчерпан — run приостанавливается с внятной причиной."""
    (tmp_path / "a.txt").write_text("содержимое", encoding="utf-8")
    call = ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "a.txt"}')
    provider = ScriptedProvider(
        [_tool_turn(call), _tool_turn(call), _tool_turn(call), _tool_turn(call), _final("готово")]
    )
    cfg = RuntimeConfig(max_iterations=10, refuel_after_iterations=2, max_refuel_rounds=1)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("работай", AutonomyMode.YOLO)

    assert outcome.state is RunState.SUSPENDED
    assert outcome.error is not None
    assert "max_refuel_rounds" in outcome.error


async def test_zero_round_cap_keeps_old_suspend_behaviour(
    db: AsyncSession, tmp_path: Path
) -> None:
    """max_refuel_rounds=0 — прежнее поведение: приостановка на первом refuel."""
    (tmp_path / "a.txt").write_text("содержимое", encoding="utf-8")
    call = ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "a.txt"}')
    provider = ScriptedProvider([_tool_turn(call), _tool_turn(call), _final("готово")])
    cfg = RuntimeConfig(max_iterations=10, refuel_after_iterations=2, max_refuel_rounds=0)
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("работай", AutonomyMode.YOLO)

    assert outcome.state is RunState.SUSPENDED
    assert outcome.error is not None
    assert "task_state.md" in outcome.error
    assert (tmp_path / "task_state.md").exists()
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `uv run pytest tests/test_loop.py -k "autocontinue or round_cap" -v`
Expected: FAIL — первые два теста падают (`SUSPENDED` вместо `COMPLETED`, отсутствие `max_refuel_rounds` в тексте); третий проходит уже сейчас.

- [ ] **Step 3: Выделить запись task_state.md**

В `src/svarog_harness/runtime/loop.py` заменить начало `_refuel_suspend` так, чтобы запись файла жила отдельным методом, и добавить этот метод рядом:

```python
    async def _write_task_state(self, run: Run, state: LoopState) -> None:
        """Сериализовать состояние задачи в task_state.md и закоммитить (§6.10).

        Вызывается и при приостановке, и при автопродолжении: файл пишется ДО
        сброса истории, поэтому падение процесса между записью и продолжением
        не теряет прогресс — resume поднимет run с уже готовым файлом.
        """
        task_state = build_task_state(state.task, state.messages, state.iterations, plan=state.plan)
        (state.workspace / task_state_path()).write_text(task_state, encoding="utf-8")
        if self._workspace_flow is not None:
            # Коммит task_state.md — лучший-эффорт (не git-репозиторий, секрет-скан…).
            with contextlib.suppress(Exception):
                await self._workspace_flow.commit_step(
                    "svarog refuel: task_state.md", run_id=run.id
                )

    async def _refuel_suspend(self, run: Run, state: LoopState) -> RunOutcome:
        """Refuel как приостановка (§6.10, ADR-0005): сбросить контекст в
        task_state.md и уйти в suspended.

        Путь для случая, когда автопродолжение выключено (max_refuel_rounds=0)
        или потолок раундов исчерпан. Раздутая история из checkpoint убирается —
        resume пересоберёт контекст с нуля из task_state.md. Процесс и sandbox
        между refuel и resume освобождаются.
        """
        await self._write_task_state(run, state)
        state.refuel_pending = True
        # Раздутую историю в checkpoint не тащим — resume пересоберёт из файла.
        state.messages = []
        state.pending_tool_calls = ()
        state.iterations_since_refuel = 0
        state.last_prompt_tokens = 0
        reason = (
            "refuel: контекст сброшен в task_state.md; "
            "выполните svarog resume для продолжения"
        )
        if self._cfg.max_refuel_rounds:
            reason = (
                f"исчерпан потолок автопродолжений "
                f"(max_refuel_rounds={self._cfg.max_refuel_rounds}); "
                f"контекст сброшен в task_state.md — поднимите потолок "
                f"или выполните svarog resume"
            )
        return await self._suspend(run, state, reason)
```

- [ ] **Step 4: Добавить автопродолжение в цикл**

В `_drive` заменить ветку refuel:

```python
                # Refuel: порог итераций сегмента достигнут — сбросить контекст
                # в task_state.md. Если потолок автопродолжений не исчерпан,
                # run продолжает себя сам (§6.10, ADR-0005, блок B §3); иначе —
                # приостановка и продолжение через svarog resume.
                if state.iterations_since_refuel >= self._cfg.refuel_after_iterations:
                    if state.refuel_rounds >= self._cfg.max_refuel_rounds:
                        return await self._refuel_suspend(run, state)
                    await self._autocontinue(run, state)
                    continue
```

И добавить метод рядом с `_rebuild_after_refuel`:

```python
    async def _autocontinue(self, run: Run, state: LoopState) -> None:
        """Сбросить контекст и продолжить run без участия человека (блок B §3).

        Порядок как при приостановке: task_state.md пишется и коммитится ДО
        сброса истории. Счётчик раундов увеличивается здесь и обнуляется только
        ручным resume — человек, продолживший run руками, выдаёт новый бюджет.
        """
        await self._write_task_state(run, state)
        state.refuel_rounds += 1
        state.refuel_pending = True
        state.pending_tool_calls = ()
        state.last_prompt_tokens = 0
        await self._rebuild_after_refuel(run, state)
        await self._recorder.merge_run_meta(run, {"refuel_rounds": state.refuel_rounds})
        if self._on_notify is not None:
            self._on_notify(
                "refuel",
                f"контекст сброшен в task_state.md, продолжаю "
                f"(раунд {state.refuel_rounds} из {self._cfg.max_refuel_rounds})",
            )
```

Реализующему: `_rebuild_after_refuel` уже выставляет `refuel_pending = False`, обнуляет `iterations_since_refuel`, пересобирает `state.messages`, пишет сообщения в trace и сохраняет checkpoint — дублировать это не нужно.

- [ ] **Step 5: Запустить тесты**

Run: `uv run pytest tests/test_loop.py tests/test_refuel.py tests/test_resume.py -v`
Expected: PASS, включая `test_max_iterations_limits_segment_not_run` из Task 2

- [ ] **Step 6: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/runtime/loop.py tests/test_loop.py
git commit -m "feat(runtime): continue run automatically after refuel"
```

---

## Task 4: Обнуление счётчика ручным resume

**Files:**
- Modify: `src/svarog_harness/runtime/loop.py` (`resume`, около строки 262)
- Test: `tests/test_resume.py`

**Interfaces:**
- Consumes: `LoopState.refuel_rounds` (Task 1), автопродолжение (Task 3).
- Produces: ничего нового.

- [ ] **Step 1: Написать падающий тест**

В `tests/test_resume.py` добавить:

```python
async def test_manual_resume_resets_refuel_round_cap(db: AsyncSession, tmp_path: Path) -> None:
    """Блок B §4: ручной resume выдаёт новый бюджет автопродолжений.

    Без обнуления resume после исчерпания потолка немедленно упирался бы
    в него снова и был бы бесполезен.
    """
    (tmp_path / "a.txt").write_text("содержимое", encoding="utf-8")
    call = ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "a.txt"}')
    cfg = RuntimeConfig(max_iterations=10, refuel_after_iterations=2, max_refuel_rounds=1)

    # Первый прогон: раунд 1 израсходован, на втором refuel потолок исчерпан.
    provider = ScriptedProvider([_tool_turn(call), _tool_turn(call), _tool_turn(call),
                                 _tool_turn(call)])
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("работай", AutonomyMode.YOLO)
    assert outcome.state is RunState.SUSPENDED

    # Ручной resume выдаёт новый бюджет: ещё один раунд и завершение.
    resumed_provider = ScriptedProvider([_tool_turn(call), _tool_turn(call), _final("готово")])
    loaded_run, raw_state = await TraceRecorder(db).load_resumable(outcome.run_id)
    resumed = await _loop(resumed_provider, db, tmp_path, cfg=cfg).resume(
        loaded_run, LoopState.from_dict(raw_state)
    )

    assert resumed.state is RunState.COMPLETED
    assert resumed.final_answer == "готово"
```

Реализующему: сценарий построен по образцу существующих resume-тестов файла (`TraceRecorder(db).load_resumable(...)` + `LoopState.from_dict(raw_state)`). Если число ходов в скриптах не сойдётся с фактическим поведением — подгоняй количество `_tool_turn(call)`, а не утверждения теста; смысл теста в том, что после ручного resume run доходит до `COMPLETED`, а не приостанавливается повторно.

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_resume.py -k resets_refuel -v`
Expected: FAIL — run снова приостанавливается вместо завершения

- [ ] **Step 3: Обнулить счётчик в resume**

В `src/svarog_harness/runtime/loop.py` в методе `resume` добавить перед `set_run_state`:

```python
        # Блок B §4: ручное продолжение выдаёт новый бюджет автопродолжений.
        # Иначе resume после исчерпания потолка упёрся бы в него немедленно.
        state.refuel_rounds = 0
```

- [ ] **Step 4: Запустить тест**

Run: `uv run pytest tests/test_resume.py -v`
Expected: PASS

- [ ] **Step 5: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/runtime/loop.py tests/test_resume.py
git commit -m "feat(runtime): reset refuel round budget on manual resume"
```

---

## Task 5: Регрессионный тест на идемпотентность resume

**Files:**
- Modify: `tests/test_resume.py`
- Test: `tests/test_resume.py`

**Interfaces:**
- Consumes: `AgentLoop.resume`, `LoopState` (существуют).
- Produces: ничего.

- [ ] **Step 1: Написать тест**

В `tests/test_resume.py` добавить:

```python
async def test_resume_history_comes_only_from_checkpoint(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Блок B §6: история при resume берётся ТОЛЬКО из блоба checkpoint'а.

    Слияния с сообщениями из trace не происходит, поэтому дублирования быть
    не может по построению. Тест запирает инвариант, который иначе держится
    только на устройстве кода.
    """
    provider = ScriptedProvider([_final("готово")])
    loop = _loop(provider, db, tmp_path)
    recorder = TraceRecorder(db)
    run = await recorder.start_run(task="задача", autonomy="yolo", model="test-model")

    messages = [
        ChatMessage(role="system", content="системный промпт"),
        ChatMessage(role="user", content="задача"),
    ]
    # В trace кладём ЛИШНЕЕ сообщение, которого нет в checkpoint: если resume
    # подмешивал бы историю из trace, модель увидела бы его.
    await recorder.add_message(run, "assistant", {"content": "постороннее из trace"})

    state = LoopState(workspace=tmp_path, messages=list(messages), task="задача")
    outcome = await loop.resume(run, state)

    assert outcome.state is RunState.COMPLETED
    sent = provider.seen_messages[0]
    assert [(m.role, m.content) for m in sent] == [(m.role, m.content) for m in messages]
    assert all("постороннее" not in m.content for m in sent)


async def test_resume_reexecutes_write_ahead_calls(db: AsyncSession, tmp_path: Path) -> None:
    """Блок B §6: незакрытые write-ahead вызовы доисполняются при resume.

    Это документированное свойство ADR-0005 (at-least-once, а не at-most-once),
    а не дефект: тест фиксирует его, чтобы будущая правка не «починила» его
    случайно.
    """
    (tmp_path / "a.txt").write_text("содержимое", encoding="utf-8")
    provider = ScriptedProvider([_final("готово")])
    loop = _loop(provider, db, tmp_path)
    recorder = TraceRecorder(db)
    run = await recorder.start_run(task="задача", autonomy="yolo", model="test-model")

    pending = ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "a.txt"}')
    state = LoopState(
        workspace=tmp_path,
        messages=[
            ChatMessage(role="system", content="системный промпт"),
            ChatMessage(role="user", content="задача"),
            ChatMessage(role="assistant", content="", tool_calls=(pending,)),
        ],
        task="задача",
        pending_tool_calls=(pending,),
    )
    outcome = await loop.resume(run, state)

    assert outcome.state is RunState.COMPLETED
    calls = (await db.scalars(select(ToolCall).where(ToolCall.run_id == run.id))).all()
    assert len(calls) == 1
    assert calls[0].tool_name == "read_file"
```

Реализующему: сверь имена хелперов (`_loop`, `_final`, `ScriptedProvider`) и импорты с тем, что уже есть в `tests/test_resume.py`; если каких-то из них там нет, возьми их из `tests/test_loop.py` тем же способом, каким это делают соседние тесты файла. Новых фикстур не заводи.

- [ ] **Step 2: Запустить тесты**

Run: `uv run pytest tests/test_resume.py -k "only_from_checkpoint or write_ahead" -v`
Expected: PASS — тесты фиксируют существующее поведение и падать не должны. Если падают, это находка: разберись и сообщи, не подгоняя тест под результат.

- [ ] **Step 3: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add tests/test_resume.py
git commit -m "test(runtime): lock resume history and write-ahead invariants"
```

---

## Task 6: Документация

**Files:**
- Modify: `docs/adr/0005-resumable-runs.md`
- Modify: `TASK.md`
- Modify: `docs/reference-analysis.md`

**Interfaces:**
- Consumes: результаты задач 1-5.
- Produces: ничего исполняемого.

- [ ] **Step 1: Дополнить ADR-0005**

Добавить раздел про автопродолжение в стиле существующих разделов документа. Зафиксировать:

- `max_iterations` теперь ограничивает **сегмент** между двумя refuel, а не весь run; прежняя формулировка про тотальный стоп-кран помечается как заменённая, а не переписывается задним числом;
- общими стоп-кранами стали потолок раундов (`runtime.max_refuel_rounds`, по умолчанию 12) и бюджеты токенов и стоимости;
- `max_refuel_rounds=0` возвращает прежнее поведение — приостановку на первом refuel;
- порядок операций не изменился: `task_state.md` пишется и коммитится до сброса истории, поэтому durability не ослабевает;
- ручной `svarog resume` обнуляет счётчик раундов: явное действие человека выдаёт новый бюджет;
- наблюдаемость: `Run.meta["refuel_rounds"]`, сообщения пересобранного контекста в trace на каждом раунде, уведомление через хук `on_notify`;
- файлы: `config/schema.py`, `runtime/checkpoint.py`, `runtime/loop.py`; тесты-репродьюсеры: `tests/test_loop.py`, `tests/test_refuel.py`, `tests/test_resume.py`.

- [ ] **Step 2: Обновить TASK.md**

Найти раздел про refuel (§6.10) и раздел с примером конфигурации, где перечислен `max_iterations` (около строки 1094), и привести в соответствие: refuel больше не обязательно требует ручного `svarog resume`; появился параметр `max_refuel_rounds`; `max_iterations` — лимит сегмента.

- [ ] **Step 3: Обновить reference-analysis**

В `docs/reference-analysis.md`, в разделе про HKUDS/nanobot, перевести блок B из «отложено» в «перенесено»:

- перенесено автопродолжение хода после сброса контекста, с потолком раундов вместо тотального лимита итераций;
- пункт «идемпотентный restore checkpoint» отвергнут как неприменимый: суффиксный матчинг nanobot решает задачу слияния checkpoint'а с существующей историей, а у Svarog `LoopState.from_dict` — единственный источник истории при resume, слияния нет. Вместо механизма инвариант закреплён тестами `tests/test_resume.py::test_resume_history_comes_only_from_checkpoint` и `::test_resume_reexecutes_write_ahead_calls`;
- отметить, что настоящая граница идемпотентности у Svarog — at-least-once для write-ahead вызовов (ADR-0005), и она в объём блока B не входила.

Проверить, что нумерация подразделов осталась связной и в списке «отложено» остались только блоки C и D.

- [ ] **Step 4: Финальная проверка и коммит**

```bash
uv run ruff check && uv run pytest
git add docs/adr/0005-resumable-runs.md TASK.md docs/reference-analysis.md
git commit -m "docs: record refuel autocontinue in ADR-0005"
```

---

## Self-Review (выполнено при написании плана)

**Покрытие спека:** §1 конфиг → Task 1; §2 семантика `max_iterations` → Task 2; §3 автопродолжение → Task 3; §4 счётчик и обнуление ручным resume → Task 1 (сериализация) и Task 4 (обнуление); §5 наблюдаемость → Task 3 Step 4 (`Run.meta["refuel_rounds"]`, `on_notify`); §6 регрессионный тест → Task 5; §7 тестирование → распределено по задачам 1-5; §8 документация → Task 6. Пропусков нет.

**Согласованность типов:** `RuntimeConfig.max_refuel_rounds` и `LoopState.refuel_rounds` объявлены в Task 1 и используются в Task 3 и 4 под теми же именами. `_write_task_state(run, state)` объявлен в Task 3 Step 3 и вызывается в Task 3 Step 3 и Step 4. `_autocontinue(run, state)` объявлен в Task 3 Step 4 и вызывается в изменённой ветке цикла там же.

**Осознанная особенность декомпозиции:** Task 2 оставляет один тест красным до Task 3 — это единственный способ разделить смену семантики лимита и само автопродолжение так, чтобы рецензент мог оценить их по отдельности. Коммит Task 2 делается вместе с Task 3; это явно написано в шаге.

**Известные места, требующие сверки с кодом при исполнении** (помечены как «Реализующему»): импорт `Path` в `tests/test_refuel.py`; существующие тесты лимита итераций и точный текст их ожиданий (Task 2 Step 4); сценарий resume-после-исчерпания-потолка (Task 4 Step 1); имена хелперов и импортов в `tests/test_resume.py` (Task 5 Step 1).
