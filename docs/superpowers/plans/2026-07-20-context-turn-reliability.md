# Блок A: контекст и надёжность хода — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перенести из HKUDS/nanobot пять механик надёжности хода, которые не покрыты текущим кодом Svarog: инвариант истории, actionable-маркер компакции, стабильный префикс tool-схем с учётом `cached_tokens`, ремонт формы tool-вызова и тайминги фаз.

**Architecture:** Все изменения точечные и живут в `runtime/`, `llm/`, `tools/`, `trace/`. Новых ADR нет — дополняется ADR-0015 новой фазой 6. Управление в `AgentLoop` не перестраивается: добавляются два новых модуля (`runtime/history_invariant.py`, `runtime/phase_timer.py`) и правки в существующих точках.

**Tech Stack:** Python 3.12+, uv, Pydantic v2, SQLAlchemy/SQLite, pytest (`asyncio_mode=auto`), ruff, mypy.

**Спек:** `docs/superpowers/specs/2026-07-20-context-turn-reliability-design.md`

## Global Constraints

- Комментарии, docstring'и, сообщения об ошибках и текст в промптах — на русском; код и идентификаторы — на английском.
- Conventional Commits, заголовок ≤72 символов, сообщение на английском в императиве: `feat|fix|docs|refactor|test|chore(scope): описание`. Scope — модуль (`runtime`, `llm`, `tools`, `trace`, `docs`).
- Перед каждым коммитом: `uv run ruff check`, `uv run ruff format`, `uv run mypy`, `uv run pytest` — всё зелёное.
- Правила зависимостей (`docs/repo-structure.md`): `cli` → `runtime` → компоненты → `storage`/`trace`. Импортов из `cli` в ядро нет.
- Никакого мёртвого кода: неиспользуемые функции, закомментированные блоки, «оставим на потом» — не проходят review.
- Секретов в коде и тестах нет; в тестах — только заведомо фейковые значения.
- Документация правится в том же наборе коммитов, что и код (Task 7).

---

## File Structure

**Создаются:**
- `src/svarog_harness/runtime/history_invariant.py` — проверка инварианта истории перед вызовом модели. Не зависит ни от чего, кроме `llm.provider`.
- `src/svarog_harness/runtime/phase_timer.py` — накопитель таймингов фаз хода. Чистая структура данных без I/O.
- `tests/test_history_invariant.py` — юнит-тесты инварианта.
- `tests/test_phase_timer.py` — юнит-тесты накопителя.

**Модифицируются:**
- `src/svarog_harness/runtime/loop.py` — вызовы инварианта и таймера, текст маркера компакции, использование `prepare_arguments`.
- `src/svarog_harness/tools/registry.py` — флаг `external`, порядок `definitions()`, `prepare_arguments`, подсказка в `UnknownToolError`.
- `src/svarog_harness/runtime/orchestrator.py:596` — регистрация MCP-инструментов с `external=True`.
- `src/svarog_harness/llm/provider.py` — поле `Usage.cached_tokens`.
- `src/svarog_harness/llm/openai_compatible.py` — чтение `cached_tokens` из трёх диалектов.
- `src/svarog_harness/trace/recorder.py` — `update_progress` принимает `cached_tokens`.
- `src/svarog_harness/trace/viewer.py:260-263` — строка с фазами и cached в `traces show`.
- `src/svarog_harness/cli/chat_inline.py:170`, `src/svarog_harness/runtime/external.py:328` — новая сигнатура `on_progress`.
- Тесты: `tests/test_loop.py`, `tests/test_resume.py`, `tests/test_deferred_tools.py`, `tests/test_llm.py`, `tests/test_cli_run_traces.py`.

---

## Task 1: Инвариант истории

**Files:**
- Create: `src/svarog_harness/runtime/history_invariant.py`
- Create: `tests/test_history_invariant.py`
- Modify: `src/svarog_harness/runtime/loop.py` (вызов перед `self._provider.complete`, ~line 280)
- Test: `tests/test_history_invariant.py`, `tests/test_resume.py`

**Interfaces:**
- Consumes: `svarog_harness.llm.provider.ChatMessage`.
- Produces: `HistoryInvariantError(RuntimeError)`; `assert_history_valid(messages: list[ChatMessage]) -> None`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_history_invariant.py`:

```python
"""Инвариант истории перед вызовом модели (блок A §1)."""

import pytest

from svarog_harness.llm.provider import ChatMessage, ToolCallRequest
from svarog_harness.runtime.history_invariant import (
    HistoryInvariantError,
    assert_history_valid,
)


def _system() -> ChatMessage:
    return ChatMessage(role="system", content="ты агент")


def _call(call_id: str) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name="read_file", arguments_json='{"path": "a.txt"}')


def test_valid_history_passes() -> None:
    messages = [
        _system(),
        ChatMessage(role="user", content="читай"),
        ChatMessage(role="assistant", tool_calls=(_call("c1"),)),
        ChatMessage(role="tool", content="результат", tool_call_id="c1"),
    ]
    assert_history_valid(messages)


def test_history_without_system_first_fails() -> None:
    with pytest.raises(HistoryInvariantError, match="system"):
        assert_history_valid([ChatMessage(role="user", content="читай")])


def test_tool_call_without_result_fails() -> None:
    messages = [
        _system(),
        ChatMessage(role="assistant", tool_calls=(_call("c1"),)),
    ]
    with pytest.raises(HistoryInvariantError, match="c1"):
        assert_history_valid(messages)


def test_tool_result_without_call_fails() -> None:
    messages = [
        _system(),
        ChatMessage(role="tool", content="результат", tool_call_id="c9"),
    ]
    with pytest.raises(HistoryInvariantError, match="c9"):
        assert_history_valid(messages)


def test_duplicate_tool_result_fails() -> None:
    messages = [
        _system(),
        ChatMessage(role="assistant", tool_calls=(_call("c1"),)),
        ChatMessage(role="tool", content="раз", tool_call_id="c1"),
        ChatMessage(role="tool", content="два", tool_call_id="c1"),
    ]
    with pytest.raises(HistoryInvariantError, match="c1"):
        assert_history_valid(messages)


def test_empty_tool_call_name_fails() -> None:
    messages = [
        _system(),
        ChatMessage(
            role="assistant",
            tool_calls=(ToolCallRequest(id="c1", name="", arguments_json="{}"),),
        ),
        ChatMessage(role="tool", content="результат", tool_call_id="c1"),
    ]
    with pytest.raises(HistoryInvariantError, match="пустое имя"):
        assert_history_valid(messages)


def test_empty_history_fails() -> None:
    with pytest.raises(HistoryInvariantError, match="пустая"):
        assert_history_valid([])
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_history_invariant.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.runtime.history_invariant'`

- [ ] **Step 3: Написать модуль**

Создать `src/svarog_harness/runtime/history_invariant.py`:

```python
"""Инвариант истории перед вызовом модели (блок A §1).

Svarog не чинит историю, как это делают харнессы без write-ahead: у него
незакрытых tool-вызовов не бывает по построению — `pending_tool_calls`
попадают в checkpoint до исполнения и доисполняются первыми при resume.
Поэтому нарушение пар «вызов ↔ результат» означает баг в логике loop'а, и
правильная реакция — упасть громко, а не подставить заглушку и замаскировать
его.
"""

from svarog_harness.llm.provider import ChatMessage


class HistoryInvariantError(RuntimeError):
    """История нарушает контракт диалога — вызов модели не выполняется."""


def assert_history_valid(messages: list[ChatMessage]) -> None:
    """Проверить историю перед отправкой модели; нарушение — HistoryInvariantError."""
    if not messages:
        raise HistoryInvariantError("пустая история: нечего отправлять модели")
    if messages[0].role != "system":
        raise HistoryInvariantError(
            f"первое сообщение истории должно быть system, получено {messages[0].role!r}"
        )

    announced: set[str] = set()
    answered: set[str] = set()
    for index, message in enumerate(messages):
        if message.role == "assistant":
            for call in message.tool_calls:
                if not call.name:
                    raise HistoryInvariantError(
                        f"сообщение [{index}]: tool call {call.id!r} с пустым именем"
                    )
                announced.add(call.id)
        elif message.role == "tool":
            call_id = message.tool_call_id
            if call_id is None:
                raise HistoryInvariantError(f"сообщение [{index}]: tool-сообщение без tool_call_id")
            if call_id not in announced:
                raise HistoryInvariantError(
                    f"сообщение [{index}]: результат ссылается на неизвестный "
                    f"tool_call_id {call_id!r}"
                )
            if call_id in answered:
                raise HistoryInvariantError(
                    f"сообщение [{index}]: повторный результат для tool_call_id {call_id!r}"
                )
            answered.add(call_id)

    missing = sorted(announced - answered)
    if missing:
        raise HistoryInvariantError(
            f"tool call без результата: {', '.join(missing)} — "
            f"баг loop'а (write-ahead должен был доисполнить вызов)"
        )
```

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `uv run pytest tests/test_history_invariant.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Подключить проверку в loop**

В `src/svarog_harness/runtime/loop.py` добавить импорт рядом с остальными импортами из `svarog_harness.runtime`:

```python
from svarog_harness.runtime.history_invariant import assert_history_valid
```

Перед вызовом провайдера (сейчас `result = await self._provider.complete(` около строки 280) вставить проверку — **после** блока микрокомпакции и присвоения `stream_callback`:

```python
                # Инвариант истории (блок A §1): нарушение — баг loop'а, а не
                # дефект модели; падаем громко, историю не правим.
                assert_history_valid(state.messages)
                result = await self._provider.complete(
```

Обработка не добавляется: внешний `except Exception` в `run()` уже переводит run в `FAILED` и кладёт причину в `Run.error`.

- [ ] **Step 6: Запустить весь набор тестов loop и resume**

Run: `uv run pytest tests/test_loop.py tests/test_resume.py tests/test_approval_flow.py -v`
Expected: PASS — все существующие тесты проходят. Если какой-то падает с `HistoryInvariantError`, это найденный реальный баг: разобраться, не глушить проверку.

- [ ] **Step 7: Добавить регрессионный тест на resume-после-approval**

В конец `tests/test_resume.py` добавить (адаптировав фикстуры под соседние тесты файла — они уже поднимают run с approval и вызывают resume):

```python
async def test_history_invariant_holds_on_resume_after_approval(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Инвариант не срабатывает ложно: pending_tool_calls доисполняются до
    первого complete() после resume, поэтому пар «вызов ↔ результат» не рвётся.

    Регрессия на блок A §1: проверка обязана стоять ПОСЛЕ доисполнения.
    """
    # Прогон до approval: run уходит в waiting_approval с write-ahead вызовом.
    ...  # использовать тот же сценарий, что и соседний тест approval-resume файла
    # После одобрения resume обязан завершиться без HistoryInvariantError.
    outcome = await _resume(db, tmp_path, run_id)
    assert outcome.state is RunState.COMPLETED
    assert outcome.error is None
```

Реализующему: взять ближайший существующий approval-resume тест в этом файле как образец построения сценария; новое здесь — только утверждение, что resume завершается без `HistoryInvariantError`.

- [ ] **Step 8: Запустить тест**

Run: `uv run pytest tests/test_resume.py -v`
Expected: PASS

- [ ] **Step 9: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/runtime/history_invariant.py src/svarog_harness/runtime/loop.py tests/test_history_invariant.py tests/test_resume.py
git commit -m "feat(runtime): assert history invariant before model call"
```

---

## Task 2: Actionable-маркер микрокомпакции

**Files:**
- Modify: `src/svarog_harness/runtime/loop.py:813`
- Test: `tests/test_loop.py`

**Interfaces:**
- Consumes: ничего нового.
- Produces: ничего нового (меняется только текст маркера).

- [ ] **Step 1: Написать падающий тест**

В `tests/test_loop.py`, в секцию `# --- ADR-0015 §1.4` после `test_microcompact_marker_references_spill_file`, добавить:

```python
async def test_microcompact_marker_is_actionable(db: AsyncSession, tmp_path: Path) -> None:
    """Без spill-файла маркер не предлагает повторить ТОТ ЖЕ вызов, а требует
    сузить параметры (блок A §2): иначе модель зацикливается на повторе."""
    big = "\n".join(f"строка {i}: " + "х" * 40 for i in range(20))  # > 500 символов
    (tmp_path / "a.txt").write_text(big, encoding="utf-8")
    (tmp_path / "b.txt").write_text(big, encoding="utf-8")
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "a.txt"}'),
                usage=Usage(600, 5),
            ),
            _tool_turn(
                ToolCallRequest(id="c2", name="read_file", arguments_json='{"path": "b.txt"}'),
                usage=Usage(600, 5),
            ),
            _final("готово"),
        ]
    )
    cfg = RuntimeConfig(
        max_context_tokens=1000,
        microcompact_threshold_ratio=0.5,
        microcompact_keep_recent=1,
    )
    outcome = await _loop(provider, db, tmp_path, cfg=cfg).run("читай", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    cleared = [m for m in provider.seen_messages[-1] if m.role == "tool"][0]
    assert "более узкими параметрами" in cleared.content
    assert "повтори вызов при необходимости" not in cleared.content
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_loop.py::test_microcompact_marker_is_actionable -v`
Expected: FAIL — `assert "более узкими параметрами" in ...`

- [ ] **Step 3: Поменять текст маркера**

В `src/svarog_harness/runtime/loop.py` заменить строку 813:

```python
            tail = f"Полный вывод: {spill.group(1)}" if spill else "повтори вызов при необходимости"
```

на:

```python
            # Компакция — обучающий сигнал, а не молчаливое усечение: маркер
            # говорит, что делать дальше, иначе модель повторяет тот же вызов.
            tail = (
                f"полный вывод: {spill.group(1)} — читай read_file частями (offset/limit)"
                if spill
                else "при необходимости повтори вызов с более узкими параметрами "
                "(путь, паттерн, лимит), а не тот же самый"
            )
```

- [ ] **Step 4: Запустить тесты микрокомпакции**

Run: `uv run pytest tests/test_loop.py -k microcompact -v`
Expected: PASS. Тест `test_microcompact_marker_references_spill_file` проверяет наличие пути к spill-файлу в маркере — он продолжает проходить, так как путь остался. Если он утверждает точную фразу `Полный вывод:` с заглавной буквы, поправить утверждение теста на новую формулировку.

- [ ] **Step 5: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/runtime/loop.py tests/test_loop.py
git commit -m "fix(runtime): make microcompaction marker actionable"
```

---

## Task 3: Стабильный порядок tool definitions

**Files:**
- Modify: `src/svarog_harness/tools/registry.py:33-53`
- Modify: `src/svarog_harness/runtime/orchestrator.py:596`
- Test: `tests/test_deferred_tools.py`

**Interfaces:**
- Consumes: ничего нового.
- Produces: `ToolRegistry.register(tool, *, deferred: bool = False, external: bool = False) -> None`; `ToolRegistry.definitions()` возвращает встроенные (sorted) → внешние (sorted) → `load_tool` последним.

- [ ] **Step 1: Написать падающий тест**

В конец `tests/test_deferred_tools.py` добавить:

```python
def test_definitions_keep_builtin_prefix_stable_when_external_added() -> None:
    """Блок A §3: встроенные идут первыми и не сдвигаются при добавлении
    MCP-инструментов — префикс промпта остаётся кэшируемым."""
    registry = ToolRegistry()
    registry.register(ReadFileTool(tmp_workspace()))
    registry.register(ListDirTool(tmp_workspace()))
    before = [d.name for d in registry.definitions()]

    registry.register(_fake_mcp_tool("mcp_alpha"), external=True)
    after = [d.name for d in registry.definitions()]

    assert after[: len(before)] == before
    assert after[-1] == "mcp_alpha"


def test_load_tool_is_always_last() -> None:
    """load_tool стоит последним: его description меняется при каждой загрузке
    deferred-схемы и не должен сдвигать ничего за собой."""
    registry = ToolRegistry()
    registry.register(ReadFileTool(tmp_workspace()))
    registry.register(_fake_mcp_tool("mcp_alpha"), deferred=True, external=True)
    registry.register(LoadToolTool(registry))

    assert [d.name for d in registry.definitions()][-1] == "load_tool"

    registry.load("mcp_alpha")
    names = [d.name for d in registry.definitions()]
    assert names[-1] == "load_tool"
    assert names[0] == "read_file"
```

Реализующему: `tmp_workspace()` и `_fake_mcp_tool()` — вспомогательные функции; в файле уже есть аналогичные фабрики для deferred-тестов, использовать их, а не заводить дубли. Если фабрики MCP-инструмента нет — добавить минимальную рядом с существующими хелперами файла.

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_deferred_tools.py -k "stable or last" -v`
Expected: FAIL — `TypeError: register() got an unexpected keyword argument 'external'`

- [ ] **Step 3: Реализовать флаг и порядок**

В `src/svarog_harness/tools/registry.py` в `__init__` добавить множество:

```python
        self._external: set[str] = set()
```

Заменить `register` и `definitions`:

```python
    def register(
        self, tool: Tool[Any], *, deferred: bool = False, external: bool = False
    ) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' уже зарегистрирован")
        self._tools[tool.name] = tool
        if deferred:
            self._deferred.add(tool.name)
        if external:
            self._external.add(tool.name)

    def definitions(self) -> list[ToolDefinition]:
        """Схемы для промпта: встроенные → внешние → load_tool.

        Порядок стабилизирует префикс промпта (блок A §3): появление и
        загрузка MCP-инструментов дописываются в хвост и не сдвигают
        встроенную часть. load_tool идёт последним намеренно — его
        description содержит сводку незагруженных deferred-схем и меняется
        при каждой загрузке.
        """
        visible = [
            name
            for name in self.names()
            if name not in self._deferred or name in self._loaded
        ]
        builtin = [n for n in visible if n not in self._external and n != _LOAD_TOOL_NAME]
        external = [n for n in visible if n in self._external]
        trailing = [n for n in visible if n == _LOAD_TOOL_NAME]
        return [self._tools[name].definition() for name in builtin + external + trailing]
```

Рядом с другими константами модуля добавить:

```python
_LOAD_TOOL_NAME = "load_tool"
```

Реализующему: сверить значение константы с атрибутом `name` у `LoadToolTool` в том же файле и использовать его, если он объявлен как атрибут класса, — дублировать строковый литерал не нужно.

- [ ] **Step 4: Запустить тест**

Run: `uv run pytest tests/test_deferred_tools.py -v`
Expected: PASS

- [ ] **Step 5: Пометить MCP-инструменты внешними**

В `src/svarog_harness/runtime/orchestrator.py:596` заменить:

```python
            registry.register(mcp_tool, deferred=defer_schemas)
```

на:

```python
            # external: MCP-схемы дописываются после встроенных, чтобы смена
            # discovery не сдвигала кэшируемый префикс промпта (блок A §3).
            registry.register(mcp_tool, deferred=defer_schemas, external=True)
```

- [ ] **Step 6: Запустить тесты MCP и deferred**

Run: `uv run pytest tests/test_mcp.py tests/test_deferred_tools.py -v`
Expected: PASS

- [ ] **Step 7: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/tools/registry.py src/svarog_harness/runtime/orchestrator.py tests/test_deferred_tools.py
git commit -m "perf(tools): keep builtin tool schema prefix stable"
```

---

## Task 4: Учёт cached_tokens

**Files:**
- Modify: `src/svarog_harness/llm/provider.py:65-72`
- Modify: `src/svarog_harness/llm/openai_compatible.py:160-165`
- Modify: `src/svarog_harness/trace/recorder.py:379-386`
- Modify: `src/svarog_harness/runtime/loop.py` (вызов `update_progress`, `on_progress`, тип hook на line 173)
- Modify: `src/svarog_harness/runtime/external.py:88,328`
- Modify: `src/svarog_harness/cli/chat_inline.py:170`
- Modify: `src/svarog_harness/trace/viewer.py:260-263`
- Test: `tests/test_llm.py`, `tests/test_loop.py`

**Interfaces:**
- Consumes: ничего нового.
- Produces: `Usage(prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0)`; `TraceRecorder.update_progress(run, *, iterations, tokens_used, cost_usd, cached_tokens: int = 0)`; hook `on_progress: Callable[[int, int, float, float, int], None]` — `(iterations, tokens, cost, context_ratio, cached_tokens)`; накопитель в `Run.meta["cached_tokens"]`.

- [ ] **Step 1: Написать падающий тест на парсинг трёх диалектов**

В `tests/test_llm.py` добавить:

```python
async def test_usage_reads_cached_tokens_from_prompt_tokens_details() -> None:
    """OpenAI/Qwen/Mistral: usage.prompt_tokens_details.cached_tokens."""
    provider = _provider_with_usage(
        prompt_tokens=100,
        completion_tokens=10,
        extra={"prompt_tokens_details": {"cached_tokens": 64}},
    )
    result = await provider.complete([ChatMessage(role="user", content="привет")], [])
    assert result.usage.cached_tokens == 64


async def test_usage_reads_top_level_cached_tokens() -> None:
    """StepFun/Moonshot: верхнеуровневый usage.cached_tokens."""
    provider = _provider_with_usage(
        prompt_tokens=100, completion_tokens=10, extra={"cached_tokens": 32}
    )
    result = await provider.complete([ChatMessage(role="user", content="привет")], [])
    assert result.usage.cached_tokens == 32


async def test_usage_reads_prompt_cache_hit_tokens() -> None:
    """DeepSeek/SiliconFlow: usage.prompt_cache_hit_tokens."""
    provider = _provider_with_usage(
        prompt_tokens=100, completion_tokens=10, extra={"prompt_cache_hit_tokens": 16}
    )
    result = await provider.complete([ChatMessage(role="user", content="привет")], [])
    assert result.usage.cached_tokens == 16


async def test_usage_without_cache_fields_is_zero() -> None:
    provider = _provider_with_usage(prompt_tokens=100, completion_tokens=10, extra={})
    result = await provider.complete([ChatMessage(role="user", content="привет")], [])
    assert result.usage.cached_tokens == 0
```

Реализующему: `_provider_with_usage(...)` — хелпер, строящий `OpenAICompatibleProvider` с подставным streaming-клиентом. В файле уже есть фейковый клиент для существующих тестов провайдера; расширить его, чтобы финальный chunk с `usage` мог нести произвольные дополнительные поля из `extra`, и завести хелпер рядом с ним.

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_llm.py -k cached -v`
Expected: FAIL — `AttributeError: 'Usage' object has no attribute 'cached_tokens'`

- [ ] **Step 3: Добавить поле в Usage**

В `src/svarog_harness/llm/provider.py` заменить класс `Usage`:

```python
@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Сколько prompt-токенов пришло из prefix cache провайдера. Без этого
    # числа эффект от стабильного префикса схем (блок A §3) нечем измерить.
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens
```

`total_tokens` намеренно не меняется: `cached_tokens` — подмножество `prompt_tokens`, а не добавка к ним.

- [ ] **Step 4: Читать три диалекта в провайдере**

В `src/svarog_harness/llm/openai_compatible.py` рядом с `_estimate_tokens` добавить:

```python
def _cached_tokens(usage: Any) -> int:
    """Прочитать cached-токены из usage — диалекты провайдеров различаются.

    OpenAI/Qwen/Mistral/Zhipu кладут их в prompt_tokens_details.cached_tokens,
    StepFun/Moonshot — верхним уровнем, DeepSeek/SiliconFlow —
    в prompt_cache_hit_tokens.
    """
    details = getattr(usage, "prompt_tokens_details", None)
    for candidate in (
        getattr(details, "cached_tokens", None) if details is not None else None,
        getattr(usage, "cached_tokens", None),
        getattr(usage, "prompt_cache_hit_tokens", None),
    ):
        if isinstance(candidate, int) and candidate > 0:
            return candidate
    return 0
```

Заменить построение `Usage` в цикле стрима (строки 161-165):

```python
            if chunk.usage is not None:
                usage = Usage(
                    prompt_tokens=chunk.usage.prompt_tokens,
                    completion_tokens=chunk.usage.completion_tokens,
                    cached_tokens=_cached_tokens(chunk.usage),
                )
```

- [ ] **Step 5: Запустить тесты провайдера**

Run: `uv run pytest tests/test_llm.py -v`
Expected: PASS

- [ ] **Step 6: Написать падающий тест на накопление в Run.meta**

В `tests/test_loop.py` добавить:

```python
async def test_cached_tokens_accumulate_in_run_meta(db: AsyncSession, tmp_path: Path) -> None:
    """Блок A §3: cached-токены копятся в Run.meta и видны в trace."""
    provider = ScriptedProvider(
        [
            _final("готово", usage=Usage(prompt_tokens=100, completion_tokens=5, cached_tokens=64)),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("задача", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    run = await db.get(Run, outcome.run_id)
    assert run is not None
    assert run.meta["cached_tokens"] == 64
```

Реализующему: если `_final(...)` в файле не принимает `usage`, использовать тот же способ задания usage, что и `_tool_turn(..., usage=Usage(600, 5))` в соседних тестах.

- [ ] **Step 7: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_loop.py::test_cached_tokens_accumulate_in_run_meta -v`
Expected: FAIL — `KeyError: 'cached_tokens'`

- [ ] **Step 8: Прокинуть cached_tokens через recorder**

В `src/svarog_harness/trace/recorder.py` заменить `update_progress`:

```python
    async def update_progress(
        self,
        run: Run,
        *,
        iterations: int,
        tokens_used: int,
        cost_usd: float,
        cached_tokens: int = 0,
    ) -> None:
        run.iterations = iterations
        run.tokens_used = tokens_used
        run.cost_usd = cost_usd
        run.heartbeat_at = utcnow()  # lease heartbeat (ADR-0015 §0.5)
        if cached_tokens:
            # JSON-колонка отслеживает только переприсваивание.
            run.meta = {**run.meta, "cached_tokens": cached_tokens}
        await self._db.commit()
```

- [ ] **Step 9: Накапливать в LoopState и передавать из loop**

В `src/svarog_harness/runtime/checkpoint.py` в `LoopState` добавить рядом с `tokens_used`:

```python
    # Сумма prompt-токенов, пришедших из prefix cache провайдера (блок A §3).
    cached_tokens: int = 0
```

В `src/svarog_harness/runtime/loop.py` после `state.tokens_used += result.usage.total_tokens` добавить:

```python
                state.cached_tokens += result.usage.cached_tokens
```

и передать в `update_progress`:

```python
                await self._recorder.update_progress(
                    run,
                    iterations=state.iterations,
                    tokens_used=state.tokens_used,
                    cost_usd=state.cost_usd,
                    cached_tokens=state.cached_tokens,
                )
```

- [ ] **Step 10: Запустить тест**

Run: `uv run pytest tests/test_loop.py::test_cached_tokens_accumulate_in_run_meta -v`
Expected: PASS

- [ ] **Step 11: Расширить hook on_progress**

В `src/svarog_harness/runtime/loop.py:173` заменить тип:

```python
        on_progress: Callable[[int, int, float, float, int], None] | None = None,
```

и вызов (около строки 299-301) — добавить пятым аргументом `state.cached_tokens`:

```python
                    self._on_progress(
                        state.iterations,
                        state.tokens_used,
                        state.cost_usd,
                        context_ratio,
                        state.cached_tokens,
                    )
```

В `src/svarog_harness/runtime/external.py:88` заменить тип на
`"Callable[[int, int, float, float, int], None] | None"`, а вызов на строке 328 — добавить `0` пятым аргументом: у внешнего агента своего учёта cached-токенов нет.

В `src/svarog_harness/cli/chat_inline.py:170` заменить сигнатуру:

```python
    def _on_progress(
        self, iterations: int, tokens: int, cost: float, ratio: float, cached: int
    ) -> None:
```

и дописать в выводимую строку `f", кэш {cached}"` — только когда `cached > 0`, чтобы не шуметь на провайдерах без кэша.

- [ ] **Step 12: Показать cached в traces show**

В `src/svarog_harness/trace/viewer.py` заменить строку «итог» (260-263):

```python
    cached = int((run.meta or {}).get("cached_tokens", 0))
    cached_suffix = f", из них {cached} из кэша" if cached else ""
    header.add_row(
        "итог",
        f"{run.iterations} итераций, {run.tokens_used} токенов{cached_suffix}, "
        f"${run.cost_usd:.4f}",
    )
```

- [ ] **Step 13: Запустить полный набор**

Run: `uv run pytest -v`
Expected: PASS. Падения в `tests/test_chat_inline.py`, `tests/test_external_executor.py`, `tests/test_cli_run_traces.py` означают, что где-то осталась старая 4-аргументная сигнатура hook'а — поправить вызов, не откатывая изменение.

- [ ] **Step 14: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add -A
git commit -m "feat(llm): track cached prompt tokens across providers"
```

---

## Task 5: Ремонт формы tool-вызова

**Files:**
- Modify: `src/svarog_harness/tools/registry.py:18-23` (подсказка) и новый метод `prepare_arguments`
- Modify: `src/svarog_harness/runtime/loop.py:851-873` (`_execute_tool`), `:428-431` (`_concurrency_safe_prefix`)
- Test: `tests/test_tools_base.py`, `tests/test_loop.py`

**Interfaces:**
- Consumes: `ToolRegistry.get`, `Tool.args_model`.
- Produces: `ToolRegistry.prepare_arguments(tool: Tool[Any], raw: str) -> tuple[dict[str, Any], list[str]]`; `UnknownToolError.suggestion: str | None`.

- [ ] **Step 1: Написать падающий тест**

В конец `tests/test_tools_base.py` добавить:

```python
def test_prepare_arguments_unwraps_double_encoded_json() -> None:
    """Модель сериализовала аргументы дважды — распаковываем (блок A §4)."""
    registry = ToolRegistry()
    tool = ReadFileTool(tmp_workspace())
    registry.register(tool)

    arguments, repairs = registry.prepare_arguments(tool, '"{\\"path\\": \\"a.txt\\"}"')

    assert arguments == {"path": "a.txt"}
    assert repairs == ["double_encoded"]


def test_prepare_arguments_unwraps_arguments_envelope() -> None:
    """Обёртка {"arguments": {...}} разворачивается, если у tool'а нет
    собственного параметра arguments."""
    registry = ToolRegistry()
    tool = ReadFileTool(tmp_workspace())
    registry.register(tool)

    arguments, repairs = registry.prepare_arguments(tool, '{"arguments": {"path": "a.txt"}}')

    assert arguments == {"path": "a.txt"}
    assert repairs == ["unwrapped"]


def test_prepare_arguments_keeps_own_arguments_parameter() -> None:
    """У tool'а есть собственный параметр arguments → обёртка НЕ разворачивается."""
    registry = ToolRegistry()
    tool = _tool_with_arguments_param()
    registry.register(tool)

    arguments, repairs = registry.prepare_arguments(tool, '{"arguments": {"path": "a.txt"}}')

    assert arguments == {"arguments": {"path": "a.txt"}}
    assert repairs == []


def test_prepare_arguments_passes_clean_input_through() -> None:
    registry = ToolRegistry()
    tool = ReadFileTool(tmp_workspace())
    registry.register(tool)

    arguments, repairs = registry.prepare_arguments(tool, '{"path": "a.txt"}')

    assert arguments == {"path": "a.txt"}
    assert repairs == []


def test_prepare_arguments_rejects_invalid_json() -> None:
    registry = ToolRegistry()
    tool = ReadFileTool(tmp_workspace())
    registry.register(tool)

    with pytest.raises(ValueError, match="JSON"):
        registry.prepare_arguments(tool, "{не json")


def test_unknown_tool_error_suggests_close_name() -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool(tmp_workspace()))

    with pytest.raises(UnknownToolError) as excinfo:
        registry.get("readfile")

    assert excinfo.value.suggestion == "read_file"
    assert "read_file" in str(excinfo.value)


def test_unknown_tool_error_lists_tools_when_no_close_name() -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool(tmp_workspace()))

    with pytest.raises(UnknownToolError) as excinfo:
        registry.get("совершенно_другое")

    assert excinfo.value.suggestion is None
    assert "доступны" in str(excinfo.value)
```

Реализующему: `tmp_workspace()` — как в соседних тестах файла; `_tool_with_arguments_param()` — минимальный `Tool` с Pydantic-моделью, у которой есть поле `arguments`. Объявить его рядом с существующими тестовыми tool'ами файла.

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_tools_base.py -k "prepare_arguments or unknown_tool" -v`
Expected: FAIL — `AttributeError: 'ToolRegistry' object has no attribute 'prepare_arguments'`

- [ ] **Step 3: Реализовать подсказку в UnknownToolError**

В `src/svarog_harness/tools/registry.py` заменить класс:

```python
class UnknownToolError(Exception):
    def __init__(self, name: str, known: list[str]) -> None:
        self.name = name
        self.suggestion = _closest_name(name, known)
        if self.suggestion is not None:
            message = f"неизвестный tool '{name}'; возможно, имелся в виду '{self.suggestion}'"
        else:
            message = f"неизвестный tool '{name}' (доступны: {', '.join(known) or 'нет'})"
        super().__init__(message)


def _normalized(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "")


def _closest_name(name: str, known: list[str]) -> str | None:
    """Ближайшее имя инструмента — только подсказка, никогда не исполнение."""
    normalized = {_normalized(candidate): candidate for candidate in known}
    exact = normalized.get(_normalized(name))
    if exact is not None:
        return exact
    matches = difflib.get_close_matches(_normalized(name), list(normalized), n=1, cutoff=0.7)
    return normalized[matches[0]] if matches else None
```

Добавить в импорты модуля `import difflib` и `from typing import Any` (если ещё нет).

- [ ] **Step 4: Реализовать prepare_arguments**

В `ToolRegistry` добавить метод:

```python
    def prepare_arguments(self, tool: Tool[Any], raw: str) -> tuple[dict[str, Any], list[str]]:
        """Разобрать аргументы вызова, починив известные дефекты формы.

        Чинятся только те искажения, которые не меняют смысл вызова: двойная
        сериализация и обёртка {"arguments": {...}} у инструмента, у которого
        нет своего параметра `arguments`. Всё остальное (невалидный JSON,
        не-объект) возвращается модели ошибкой. Список выполненных ремонтов
        уходит в trace — молчаливая нормализация ломала бы свойство «trace
        отвечает, что именно исполнялось».
        """
        repairs: list[str] = []
        parsed = json.loads(raw) if raw else {}

        if isinstance(parsed, str):
            try:
                inner = json.loads(parsed)
            except json.JSONDecodeError:
                inner = None
            if isinstance(inner, dict):
                parsed = inner
                repairs.append("double_encoded")

        if not isinstance(parsed, dict):
            raise ValueError(
                f"аргументы tool call должны быть JSON-объектом, получен {type(parsed).__name__}"
            )

        envelope = parsed.get("arguments")
        if (
            list(parsed) == ["arguments"]
            and isinstance(envelope, dict)
            and "arguments" not in tool.args_model.model_fields
        ):
            parsed = envelope
            repairs.append("unwrapped")

        return parsed, repairs
```

Добавить в импорты модуля `import json`. Невалидный JSON поднимается как `json.JSONDecodeError`, который является подклассом `ValueError`, — вызывающий код в loop уже ловит `ValueError`.

- [ ] **Step 5: Запустить тест**

Run: `uv run pytest tests/test_tools_base.py -v`
Expected: PASS

- [ ] **Step 6: Написать падающий тест на видимость ремонта в trace**

В `tests/test_loop.py` добавить:

```python
async def test_repaired_call_records_original_in_trace(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Блок A §4: ремонт формы аргументов виден в trace — и что прислала
    модель, и что реально исполнилось."""
    (tmp_path / "a.txt").write_text("содержимое", encoding="utf-8")
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(
                    id="c1",
                    name="read_file",
                    arguments_json='{"arguments": {"path": "a.txt"}}',
                )
            ),
            _final("готово"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("читай", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    calls = (await db.scalars(select(ToolCall).where(ToolCall.run_id == outcome.run_id))).all()
    assert len(calls) == 1
    assert calls[0].status is ToolCallStatus.SUCCEEDED
    assert calls[0].arguments["path"] == "a.txt"
    assert calls[0].arguments["_repairs"] == ["unwrapped"]
    assert "arguments" in calls[0].arguments["_raw"]
```

- [ ] **Step 7: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_loop.py::test_repaired_call_records_original_in_trace -v`
Expected: FAIL — вызов завершается ошибкой валидации, `_repairs` в trace отсутствует

- [ ] **Step 8: Подключить ремонт в loop**

В `src/svarog_harness/runtime/loop.py` в `_execute_tool` заменить начало метода (строки 851-873):

```python
    async def _execute_tool(self, run: Run, call: ToolCallRequest) -> ToolResult:
        try:
            tool = self._registry.get(call.name)
        except UnknownToolError as exc:
            record = await self._recorder.start_tool_call(
                run,
                tool_name=call.name,
                arguments={"_raw": self._redact_text(call.arguments_json)},
                risk_level=None,
            )
            result = ToolResult.failure(str(exc))
            await self._recorder.finish_tool_call(record, ok=False, output="", error=result.error)
            return result

        try:
            arguments, repairs = self._registry.prepare_arguments(tool, call.arguments_json)
        except ValueError as exc:
            record = await self._recorder.start_tool_call(
                run,
                tool_name=call.name,
                arguments={"_raw": self._redact_text(call.arguments_json)},
                risk_level=None,
            )
            result = ToolResult.failure(str(exc))
            await self._recorder.finish_tool_call(record, ok=False, output="", error=result.error)
            return result
```

Порядок изменился намеренно: `prepare_arguments` требует `tool` (проверка наличия собственного параметра `arguments`), поэтому разрешение имени идёт первым.

Ниже, в местах, где `arguments` уходит в `start_tool_call`, использовать общий помощник — добавить его в класс:

```python
    def _traced_arguments(
        self, call: ToolCallRequest, arguments: dict[str, Any], repairs: list[str]
    ) -> dict[str, Any]:
        """Аргументы для trace: при ремонте показываем и оригинал, и результат."""
        traced = self._redact_json(arguments)
        if repairs:
            traced = {
                **traced,
                "_repairs": repairs,
                "_raw": self._redact_text(call.arguments_json),
            }
        return traced
```

и заменить в `_execute_tool` каждый `arguments=self._redact_json(arguments)` на
`arguments=self._traced_arguments(call, arguments, repairs)`.

В `_concurrency_safe_prefix` (строки 428-431) заменить:

```python
            try:
                arguments = call.parse_arguments()
                tool = self._registry.get(call.name)
            except (ValueError, UnknownToolError):
                break
```

на:

```python
            try:
                tool = self._registry.get(call.name)
                arguments, repairs = self._registry.prepare_arguments(tool, call.arguments_json)
            except (ValueError, UnknownToolError):
                break
```

и пробросить `repairs` в `_PreparedCall`: добавить поле

```python
    repairs: list[str]
```

в dataclass `_PreparedCall` (после `arguments`), передавать `_PreparedCall(call, arguments, repairs, tool, decision)` и в `_execute_batch` использовать `arguments=self._traced_arguments(prepared.call, prepared.arguments, prepared.repairs)`.

- [ ] **Step 9: Запустить тесты**

Run: `uv run pytest tests/test_loop.py tests/test_tools_base.py -v`
Expected: PASS

- [ ] **Step 10: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/tools/registry.py src/svarog_harness/runtime/loop.py tests/test_tools_base.py tests/test_loop.py
git commit -m "feat(tools): repair malformed tool call arguments"
```

---

## Task 6: Тайминги фаз хода

**Files:**
- Create: `src/svarog_harness/runtime/phase_timer.py`
- Create: `tests/test_phase_timer.py`
- Modify: `src/svarog_harness/runtime/loop.py`
- Modify: `src/svarog_harness/trace/viewer.py`
- Test: `tests/test_phase_timer.py`, `tests/test_loop.py`

**Interfaces:**
- Consumes: ничего нового.
- Produces: `PhaseTimer` с методами `measure(phase: str) -> ContextManager[None]`, `as_meta() -> dict[str, Any]`, `restore(meta: dict[str, Any]) -> None`; ключ `Run.meta["phases"]` вида `{"llm_call": {"ms": 1234, "count": 3}, ..., "last": "tool_exec"}`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_phase_timer.py`:

```python
"""Накопитель таймингов фаз хода (блок A §5)."""

from svarog_harness.runtime.phase_timer import PhaseTimer


def test_measure_accumulates_time_and_count() -> None:
    timer = PhaseTimer()
    with timer.measure("llm_call"):
        pass
    with timer.measure("llm_call"):
        pass

    meta = timer.as_meta()
    assert meta["llm_call"]["count"] == 2
    assert meta["llm_call"]["ms"] >= 0


def test_last_phase_tracks_most_recent() -> None:
    timer = PhaseTimer()
    with timer.measure("llm_call"):
        pass
    with timer.measure("tool_exec"):
        pass

    assert timer.as_meta()["last"] == "tool_exec"


def test_last_phase_survives_exception() -> None:
    """Фаза, на которой упал ход, остаётся видна — это и есть «где встал run»."""
    timer = PhaseTimer()
    try:
        with timer.measure("tool_exec"):
            raise RuntimeError("сбой")
    except RuntimeError:
        pass

    meta = timer.as_meta()
    assert meta["last"] == "tool_exec"
    assert meta["tool_exec"]["count"] == 1


def test_restore_continues_accumulating_after_resume() -> None:
    timer = PhaseTimer()
    timer.restore({"llm_call": {"ms": 500, "count": 2}, "last": "llm_call"})
    with timer.measure("llm_call"):
        pass

    meta = timer.as_meta()
    assert meta["llm_call"]["count"] == 3
    assert meta["llm_call"]["ms"] >= 500


def test_restore_ignores_malformed_meta() -> None:
    """Чужой или испорченный meta не должен ронять run."""
    timer = PhaseTimer()
    timer.restore({"llm_call": "мусор", "last": 42})
    with timer.measure("llm_call"):
        pass

    assert timer.as_meta()["llm_call"]["count"] == 1
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_phase_timer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.runtime.phase_timer'`

- [ ] **Step 3: Написать модуль**

Создать `src/svarog_harness/runtime/phase_timer.py`:

```python
"""Тайминги фаз хода (блок A §5).

Отвечает на вопрос «где встал run» и «куда ушло время», не перестраивая
управление в AgentLoop: фазы — это уже существующие участки цикла. Агрегат
живёт в Run.meta, поэтому переживает resume и не требует миграции.

approval_wait измеряется отдельной фазой и не смешивается с остальными:
ожидание решения человека измеряется часами и исказило бы любую сумму.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class PhaseTimer:
    def __init__(self) -> None:
        self._phases: dict[str, dict[str, int]] = {}
        self._last: str = ""

    @contextmanager
    def measure(self, phase: str) -> Iterator[None]:
        """Замерить участок; фаза засчитывается даже при исключении внутри."""
        started = time.monotonic()
        self._last = phase
        try:
            yield
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            entry = self._phases.setdefault(phase, {"ms": 0, "count": 0})
            entry["ms"] += elapsed_ms
            entry["count"] += 1

    def as_meta(self) -> dict[str, Any]:
        """Снимок для Run.meta['phases']."""
        meta: dict[str, Any] = {name: dict(entry) for name, entry in self._phases.items()}
        meta["last"] = self._last
        return meta

    def restore(self, meta: dict[str, Any]) -> None:
        """Восстановить агрегат после resume; испорченные записи пропускаются."""
        for name, entry in meta.items():
            if name == "last":
                if isinstance(entry, str):
                    self._last = entry
                continue
            if not isinstance(entry, dict):
                continue
            ms = entry.get("ms")
            count = entry.get("count")
            if isinstance(ms, int) and isinstance(count, int):
                self._phases[name] = {"ms": ms, "count": count}
```

- [ ] **Step 4: Запустить тест**

Run: `uv run pytest tests/test_phase_timer.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Написать падающий тест на запись фаз в Run.meta**

В `tests/test_loop.py` добавить:

```python
async def test_phase_timings_land_in_run_meta(db: AsyncSession, tmp_path: Path) -> None:
    """Блок A §5: тайминги фаз пишутся в Run.meta и переживают завершение run."""
    (tmp_path / "a.txt").write_text("содержимое", encoding="utf-8")
    provider = ScriptedProvider(
        [
            _tool_turn(ToolCallRequest(id="c1", name="read_file", arguments_json='{"path": "a.txt"}')),
            _final("готово"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("читай", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    run = await db.get(Run, outcome.run_id)
    assert run is not None
    phases = run.meta["phases"]
    assert phases["llm_call"]["count"] == 2
    assert phases["tool_exec"]["count"] >= 1
    assert phases["last"]
```

- [ ] **Step 6: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_loop.py::test_phase_timings_land_in_run_meta -v`
Expected: FAIL — `KeyError: 'phases'`

- [ ] **Step 7: Подключить таймер в loop**

В `src/svarog_harness/runtime/loop.py` добавить импорт:

```python
from svarog_harness.runtime.phase_timer import PhaseTimer
```

В начале `run()` (и в `resume`-пути, где восстанавливается состояние) создать таймер и восстановить агрегат:

```python
        phases = PhaseTimer()
        phases.restore(dict((run.meta or {}).get("phases", {})))
```

Обернуть участки:

```python
                with phases.measure("microcompact"):
                    if self._should_microcompact(state):
                        self._microcompact(state)
```

```python
                with phases.measure("llm_call"):
                    result = await self._provider.complete(...)
```

```python
                with phases.measure("tool_exec"):
                    had_tool_success = await self._execute_pending(run, state)
```

Внутри `_execute_pending` дополнительных фаз не заводить: `memory_flush` и `checkpoint` измеряются там же, где вызываются, — обернуть `await self._flush_memory(run)` в `phases.measure("memory_flush")` и `await self._save_checkpoint(run, state)` в `phases.measure("checkpoint")`, передав таймер в метод параметром.

Ожидание approval обернуть в `_wait_for_approval`:

```python
            with phases.measure("approval_wait"):
                ...
```

Писать агрегат вместе с прогрессом — в том же месте, где вызывается `update_progress`:

```python
                await self._recorder.merge_run_meta(run, {"phases": phases.as_meta()})
```

Реализующему: `merge_run_meta` уже существует (`recorder.py:388`) и переприсваивает `Run.meta` целиком — мутировать словарь на месте нельзя.

- [ ] **Step 8: Запустить тест**

Run: `uv run pytest tests/test_loop.py::test_phase_timings_land_in_run_meta -v`
Expected: PASS

- [ ] **Step 9: Показать фазы в traces show**

В `src/svarog_harness/trace/viewer.py` после строки «итог» добавить:

```python
    phases = (run.meta or {}).get("phases") or {}
    if phases:
        parts = [
            f"{name} {entry['ms']}мс×{entry['count']}"
            for name, entry in sorted(phases.items())
            if isinstance(entry, dict)
        ]
        header.add_row("фазы", ", ".join(parts) + f" | последняя: {phases.get('last', '?')}")
```

- [ ] **Step 10: Проверить вывод вручную и прогнать тесты CLI**

Run: `uv run pytest tests/test_cli_run_traces.py tests/test_cli_json.py -v`
Expected: PASS. `traces show --json` получает фазы автоматически — `run_to_dict` уже отдаёт `run.meta` целиком.

- [ ] **Step 11: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/runtime/phase_timer.py src/svarog_harness/runtime/loop.py src/svarog_harness/trace/viewer.py tests/test_phase_timer.py tests/test_loop.py
git commit -m "feat(runtime): record per-phase turn timings in run meta"
```

---

## Task 7: Документация

**Files:**
- Modify: `docs/adr/0015-runtime-hardening-and-context-economy.md`
- Modify: `docs/reference-analysis.md`
- Modify: `TASK.md`

**Interfaces:**
- Consumes: результаты задач 1-6.
- Produces: ничего исполняемого.

- [ ] **Step 1: Дописать фазу 6 в ADR-0015**

В таблицу статусов в начале документа добавить строки в том же формате, что и существующие:

```markdown
| Фаза 6.1 инвариант истории | ✅ Сделано |
| Фаза 6.2 actionable-маркер компакции | ✅ Сделано |
| Фаза 6.3 стабильный префикс схем + cached_tokens | ✅ Сделано |
| Фаза 6.4 ремонт формы tool-вызова | ✅ Сделано |
| Фаза 6.5 тайминги фаз хода | ✅ Сделано |
```

В конец документа добавить раздел «Фаза 6 — заимствования из nanobot» с заметками о scoping в стиле существующих фаз: по абзацу на пункт, с указанием файлов и reproducer'ов (`tests/test_history_invariant.py`, `tests/test_phase_timer.py`, `tests/test_tools_base.py`, `tests/test_loop.py`, `tests/test_deferred_tools.py`, `tests/test_llm.py`, `tests/test_resume.py`).

Обязательно зафиксировать три решения и их причины, иначе они будут пересмотрены заново:
- ремонт истории (orphan/backfill) **не** переносится — write-ahead делает orphan невозможным, вместо ремонта стоит проверка инварианта;
- FSM с таблицей переходов **не** вводится — у Svarog один вход в ход;
- `ErrorEvent` для нарушения инварианта **не** используется — у таблицы нет ни одного писателя.

- [ ] **Step 2: Добавить раздел про nanobot в reference-analysis**

В `docs/reference-analysis.md` добавить раздел «3. HKUDS/nanobot» (сейчас документ покрывает только hermes-agent и OpenHarness): лицензия MIT, что перенесено (пять пунктов блока A со ссылками на файлы nanobot), что отложено в блоки B-E (автопродолжение хода, идемпотентный restore, Dream, cron/heartbeat/триггеры, boundary note, Runtime Context, tmpfs-маскировка), и что отвергнуто с причинами (каналы/WebUI целиком, инструмент `my` — противоречит заморозке policy при старте run, bwrap внутри Docker — требует `CAP_SYS_ADMIN` и размывает изоляцию контейнера, fallback-цепочки и tiktoken-оценка — исключены из объёма решением 2026-07-20).

- [ ] **Step 3: Обновить затронутые разделы TASK.md**

Найти разделы, описывающие экономику контекста и исполнение tools (§6.10 refuel, §3.7 бюджеты, разделы про tool-вызовы и trace), и привести их в соответствие: упомянуть инвариант истории перед вызовом модели, стабильный порядок схем, `cached_tokens` в учёте, ремонт формы аргументов с записью в trace, тайминги фаз в `Run.meta`.

- [ ] **Step 4: Финальная проверка и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add docs/adr/0015-runtime-hardening-and-context-economy.md docs/reference-analysis.md TASK.md
git commit -m "docs: record block A phase in ADR-0015 and nanobot analysis"
```

---

## Self-Review (выполнено при написании плана)

**Покрытие спека:** §1 → Task 1; §2 → Task 2; §3 → Task 3 (порядок) + Task 4 (cached_tokens); §4 → Task 5; §5 → Task 6; «Тестирование» → регрессионные тесты в Task 1 Step 7 и Task 3 Step 1; «Документация» → Task 7. Пропусков нет.

**Согласованность типов:** `Usage.cached_tokens` (Task 4 Step 3) используется в Task 4 Step 4 и Step 9. `ToolRegistry.prepare_arguments` (Task 5 Step 4) вызывается в Task 5 Step 8 с той же сигнатурой. `PhaseTimer.measure/as_meta/restore` (Task 6 Step 3) используются в Task 6 Step 7 и Step 9. `_traced_arguments` объявляется и используется внутри Task 5.

**Известные места, требующие сверки с кодом при исполнении** (помечены в шагах как «Реализующему»): имена тестовых фабрик в `tests/test_deferred_tools.py`, `tests/test_tools_base.py`, `tests/test_llm.py`; способ задания `usage` в `_final(...)` в `tests/test_loop.py`; сценарий approval-resume в `tests/test_resume.py`; константа имени `load_tool`.
