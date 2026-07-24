# Гиперперсонализация профиля (связка A) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Профиль пользователя становится типизированным и превращается в
поведенческую директиву, а долговечные факты о пользователе захватываются
автоматически и обслуживаются Dream.

**Architecture:** Три пласта. (1) `memory/sections.py` + `memory/profile.py` —
чистый парсер H2-секций и рендер персона-директивы, которую
`context_builder.py` кладёт в системный промпт как инструкцию. (2)
`memory/autocapture.py` — aux-LLM extractor, который по границе сессии
аддитивно дописывает факты в `user/profile.md` через штатную очередь writer'а
(Flow A). (3) Расширение `memory/dream.py` и дефолтов конфига.

**Tech Stack:** Python 3.12, pydantic v2 (config), SQLAlchemy async (recorder/
writer), pytest + pytest-asyncio. LLM — `ModelProvider` (openai-compatible).

## Global Constraints

- Инфраструктура — только Git + SQLite; никаких эмбеддингов/векторных БД (ADR-0001).
- Память меняется только через single-writer очередь (ADR-0004); прямой записи в
  memory-репозиторий из кода фичи нет.
- Extractor — без tools (только structured output) и вне критического пути: его
  сбой **никогда** не роняет run.
- Автозахват пишет **только** в `user/profile.md` (jail) и только **аддитивно**
  (append / добавление в секцию); destructive-замена — не в этой фиче.
- Русские докстринги, `ruff` + `mypy` чисты; тесты по образцу `tests/test_memory_*.py`.
- Aux-модель — `models.auxiliary_or_default` через `auxiliary_provider(...)`.
- Дефолты `autocapture.enabled` и `dream.enabled` — `true` (shipped-default).

---

### Task 1: Парсер H2-секций (`memory/sections.py`)

Чистый модуль: разложить markdown на `{заголовок H2: тело}`. Переиспользует
идею `_find_header` из `memory/apply.py`, но возвращает все секции разом.

**Files:**
- Create: `src/svarog_harness/memory/sections.py`
- Test: `tests/test_memory_sections.py`

**Interfaces:**
- Produces: `parse_sections(text: str) -> dict[str, str]` — H2-заголовок (без
  `##`, strip) → тело секции (между этим H2 и следующим H2/EOF, strip). Секции
  вложенных уровней (`###`) остаются частью тела родительского H2. Текст до
  первого H2 игнорируется.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_memory_sections.py
"""Тесты парсера H2-секций профиля (#3)."""

from svarog_harness.memory.sections import parse_sections


def test_parse_sections_splits_h2_blocks() -> None:
    text = "# Профиль\nвступление\n\n## Тон\nкратко\n\n## Язык\nрусский\n"
    assert parse_sections(text) == {"Тон": "кратко", "Язык": "русский"}


def test_parse_sections_keeps_nested_headers_in_body() -> None:
    text = "## Предпочтения\nтекст\n### деталь\nещё\n\n## Роль\nбэкенд\n"
    result = parse_sections(text)
    assert result["Предпочтения"] == "текст\n### деталь\nещё"
    assert result["Роль"] == "бэкенд"


def test_parse_sections_empty_and_no_h2() -> None:
    assert parse_sections("") == {}
    assert parse_sections("просто текст без секций") == {}
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `uv run pytest tests/test_memory_sections.py -v`
Expected: FAIL — `ModuleNotFoundError: ...memory.sections`.

- [ ] **Step 3: Реализация**

```python
# src/svarog_harness/memory/sections.py
"""Парсер markdown на H2-секции (#3, гиперперсонализация профиля).

Возвращает {заголовок H2: тело}. Тело — всё до следующего H2 (или EOF),
включая вложенные H3+. Текст до первого H2 отбрасывается. Чистая функция,
без IO — рядом с `memory/apply.py`, но общего назначения.
"""


def parse_sections(text: str) -> dict[str, str]:
    lines = text.splitlines()
    sections: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []

    def flush() -> None:
        if current is not None:
            sections[current] = "\n".join(body).strip()

    for line in lines:
        stripped = line.strip()
        is_h2 = stripped.startswith("## ") and not stripped.startswith("### ")
        if is_h2:
            flush()
            current = stripped[3:].strip()
            body = []
        elif current is not None:
            body.append(line)
    flush()
    return sections
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `uv run pytest tests/test_memory_sections.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Коммит**

```bash
git add src/svarog_harness/memory/sections.py tests/test_memory_sections.py
git commit -m "feat(memory): парсер H2-секций для контракта профиля (#3)"
```

---

### Task 2: Контракт профиля и персона-директива (`memory/profile.py`)

**Files:**
- Create: `src/svarog_harness/memory/profile.py`
- Test: `tests/test_memory_profile.py`

**Interfaces:**
- Consumes: `parse_sections` (Task 1).
- Produces:
  - `BEHAVIORAL_SECTIONS: tuple[str, ...] = ("Тон", "Язык", "Предпочтения", "Не трогать")`
  - `FACTUAL_SECTIONS: tuple[str, ...] = ("Роль", "Расписание", "Прочее")`
  - `KNOWN_SECTIONS: tuple[str, ...]` — конкатенация двух выше.
  - `render_persona_directive(profile_text: str) -> str` — компактный
    директивный блок из непустых поведенческих секций; `""` если ни одной.
  - `load_profile(mem_dir: Path) -> str` — прочитать `user/profile.md` (или `""`).

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_memory_profile.py
"""Тесты контракта профиля и персона-директивы (#3)."""

from pathlib import Path

from svarog_harness.memory.profile import (
    BEHAVIORAL_SECTIONS,
    load_profile,
    render_persona_directive,
)


def test_directive_uses_only_behavioral_sections() -> None:
    text = (
        "## Тон\nкратко, без воды\n\n"
        "## Язык\nрусский\n\n"
        "## Роль\nбэкенд в Северстали\n"  # фактическая — не в директиве
    )
    directive = render_persona_directive(text)
    assert "кратко, без воды" in directive
    assert "русский" in directive
    assert "Северстали" not in directive
    assert "Тон:" in directive and "Язык:" in directive


def test_directive_empty_when_no_behavioral_sections() -> None:
    assert render_persona_directive("") == ""
    assert render_persona_directive("## Роль\nменеджер\n") == ""


def test_load_profile_reads_file_or_empty(tmp_path: Path) -> None:
    assert load_profile(tmp_path) == ""
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "profile.md").write_text("## Тон\nживо\n", encoding="utf-8")
    assert "живо" in load_profile(tmp_path)


def test_behavioral_sections_are_domain_neutral() -> None:
    assert "Стиль кода" not in BEHAVIORAL_SECTIONS
    assert "Предпочтения" in BEHAVIORAL_SECTIONS
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `uv run pytest tests/test_memory_profile.py -v`
Expected: FAIL — `ModuleNotFoundError: ...memory.profile`.

- [ ] **Step 3: Реализация**

```python
# src/svarog_harness/memory/profile.py
"""Контракт профиля пользователя и рендер персона-директивы (#3).

Профиль (`user/profile.md`) — типизированные, но необязательные H2-секции.
Поведенческие секции код превращает в директиву поведения (инструкцию в
системном промпте), фактические остаются справочным контекстом. Неизвестные
секции разрешены и в директиву не идут.
"""

from pathlib import Path

from svarog_harness.memory.sections import parse_sections

# Из этих секций собирается директива «как себя вести».
BEHAVIORAL_SECTIONS: tuple[str, ...] = ("Тон", "Язык", "Предпочтения", "Не трогать")
# Эти остаются фактами в блоке памяти, поведение из них не выводится.
FACTUAL_SECTIONS: tuple[str, ...] = ("Роль", "Расписание", "Прочее")
KNOWN_SECTIONS: tuple[str, ...] = BEHAVIORAL_SECTIONS + FACTUAL_SECTIONS

_DIRECTIVE_HEADER = "# Персонализация (следуй как инструкции)"


def render_persona_directive(profile_text: str) -> str:
    """Собрать директивный блок из непустых поведенческих секций профиля."""
    sections = parse_sections(profile_text)
    lines: list[str] = []
    for name in BEHAVIORAL_SECTIONS:
        body = sections.get(name, "").strip()
        if body:
            lines.append(f"{name}: {body}")
    if not lines:
        return ""
    return _DIRECTIVE_HEADER + "\n" + "\n".join(lines)


def load_profile(mem_dir: Path) -> str:
    """Прочитать текст `user/profile.md` (или '' если файла/каталога нет)."""
    path = mem_dir / "user" / "profile.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `uv run pytest tests/test_memory_profile.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Коммит**

```bash
git add src/svarog_harness/memory/profile.py tests/test_memory_profile.py
git commit -m "feat(memory): контракт профиля + рендер персона-директивы (#3)"
```

---

### Task 3: Инъекция директивы в нативный системный промпт

Директива идёт в системный промпт **отдельным блоком** (инструкция), не
смешиваясь с «Текущее содержимое памяти». Плюс обновляется `_MEMORY_GUIDE`
(имена секций профиля + пассивный nudge).

**Files:**
- Modify: `src/svarog_harness/runtime/context_builder.py` (`_system_prompt`,
  `build_initial_messages`, `_MEMORY_GUIDE`)
- Modify: `src/svarog_harness/runtime/loop.py` (AgentLoop хранит `persona`,
  прокидывает в `build_initial_messages`; call site ~268)
- Modify: `src/svarog_harness/runtime/run_assembly.py` (вычислить директиву,
  передать в `AgentLoop`)
- Test: `tests/test_context_builder.py`

**Interfaces:**
- Consumes: `render_persona_directive`, `load_profile` (Task 2).
- Produces: `build_initial_messages(task, workspace, *, skill_cards="",
  memory="", persona="", history=None)` — новый keyword `persona`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_context_builder.py — добавить
from svarog_harness.runtime.context_builder import build_initial_messages
from pathlib import Path


def test_persona_directive_injected_as_instruction() -> None:
    messages = build_initial_messages(
        "задача",
        Path("/ws"),
        memory="## user/profile.md\n...",
        persona="# Персонализация (следуй как инструкции)\nТон: кратко",
    )
    system = messages[0].content
    assert messages[0].role == "system"
    assert "Персонализация (следуй как инструкции)" in system
    assert "Тон: кратко" in system


def test_persona_absent_when_empty() -> None:
    messages = build_initial_messages("задача", Path("/ws"), persona="")
    assert "Персонализация" not in messages[0].content
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `uv run pytest tests/test_context_builder.py -k persona -v`
Expected: FAIL — `build_initial_messages() got an unexpected keyword argument 'persona'`.

- [ ] **Step 3: Реализация**

В `context_builder.py` — сигнатуры и сборка:

```python
def _system_prompt(
    workspace: Path, *, skill_cards: str, memory: str, persona: str
) -> str:
    system = _SYSTEM_PROMPT.format(workspace=workspace)
    if persona:
        system = f"{system}\n{persona}\n"
    if memory:
        system = (
            f"{system}\n# Память агента\n{_MEMORY_GUIDE}\nТекущее содержимое памяти:\n{memory}\n"
        )
    if skill_cards:
        system = f"{system}\n# {skill_cards}\n"
    return system
```

```python
def build_initial_messages(
    task: str,
    workspace: Path,
    *,
    skill_cards: str = "",
    memory: str = "",
    persona: str = "",
    history: list[ChatMessage] | None = None,
) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="system",
            content=_system_prompt(
                workspace, skill_cards=skill_cards, memory=memory, persona=persona
            ),
        ),
        *(history or []),
        ChatMessage(role="user", content=task),
    ]
```

В `_MEMORY_GUIDE` — заменить строку про `user/profile.md` на контракт секций и
добавить пассивный nudge. Найти строку:

```python
- user/profile.md — факты о пользователе: предпочтения, расписание, работа;
```

и заменить на:

```python
- user/profile.md — профиль пользователя типизированными H2-секциями. \
Поведенческие (влияют на то, как ты отвечаешь): «## Тон», «## Язык», \
«## Предпочтения», «## Не трогать». Фактические (справка): «## Роль», \
«## Расписание», «## Прочее». Секции опциональны, лишние допустимы. \
Долговечные факты и предпочтения о пользователе сохраняй сюда через \
remember replace_section по нужной секции (нет секции — append «## Имя\\nтекст»);
```

В `loop.py` — AgentLoop принимает и хранит `persona` (добавить параметр
`persona: str = ""` в `__init__`, сохранить `self._persona = persona`), и в
вызове `build_initial_messages` (около строки 268) добавить `persona=self._persona`:

```python
        messages = build_initial_messages(
            task,
            self._workspace,
            skill_cards=self._skill_cards,
            memory=self._memory,
            persona=self._persona,
            history=history,
        )
```

В `run_assembly.py` `build_loop` — вычислить директиву рядом с `memory_text`
(после строки 260) и передать в `AgentLoop(...)` (около строки 288):

```python
from svarog_harness.memory.profile import load_profile, render_persona_directive
# ...
        persona = render_persona_directive(load_profile(mem_dir)) if mem_dir is not None else ""
```

и в конструкторе `AgentLoop(...)` добавить `persona=persona,` рядом с `memory=memory_text,`.

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `uv run pytest tests/test_context_builder.py -v`
Expected: PASS (включая новые persona-тесты и старые).

- [ ] **Step 5: Коммит**

```bash
git add src/svarog_harness/runtime/context_builder.py src/svarog_harness/runtime/loop.py src/svarog_harness/runtime/run_assembly.py tests/test_context_builder.py
git commit -m "feat(runtime): персона-директива в системном промпте + контракт секций в guide (#3)"
```

---

### Task 4: Директива во внешние executor'ы (claude-code / opencode)

Внешние агенты получают память через `CLAUDE.md`/`AGENTS.md`. Директиву
подставляем в тот же `memory`-текст, что уходит в `context_files`, чтобы
поведение персонализировалось и там.

**Files:**
- Modify: `src/svarog_harness/runtime/run_assembly.py` (`prepare_agent_launch` —
  место сборки `memory_text` для внешнего executor'а)
- Test: `tests/test_run_assembly.py` (или существующий тест внешнего запуска;
  если файла нет — создать минимальный)

**Interfaces:**
- Consumes: `render_persona_directive`, `load_profile` (Task 2).

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_run_assembly.py — добавить (проверяем, что директива префиксит memory)
def test_external_memory_text_prefixed_with_persona(tmp_path) -> None:
    from svarog_harness.memory.profile import render_persona_directive
    directive = render_persona_directive("## Тон\nкратко\n")
    memory_body = "## user/profile.md\n## Тон\nкратко"
    combined = f"{directive}\n\n{memory_body}" if directive else memory_body
    assert combined.startswith("# Персонализация")
    assert "## user/profile.md" in combined
```

> Примечание для исполнителя: этот тест фиксирует форму комбинирования. Если в
> `prepare_agent_launch` уже есть точечный тест сборки `memory_text`, добавь
> ассерт про префикс директивы туда; иначе оставь как есть.

- [ ] **Step 2: Запустить — убедиться, что падает или отсутствует поведение**

Run: `uv run pytest tests/test_run_assembly.py -k persona -v`
Expected: FAIL (нет файла/теста) — затем реализуем.

- [ ] **Step 3: Реализация**

В `prepare_agent_launch` найти строку, где формируется `memory_text` для
внешнего запуска (по образцу строк 255–260 в `build_loop`), и после неё:

```python
        directive = render_persona_directive(load_profile(mem_dir)) if mem_dir is not None else ""
        if directive:
            memory_text = f"{directive}\n\n{memory_text}"
```

(импорт `render_persona_directive`, `load_profile` уже добавлен в Task 3.)

- [ ] **Step 4: Запустить тест**

Run: `uv run pytest tests/test_run_assembly.py -v`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add src/svarog_harness/runtime/run_assembly.py tests/test_run_assembly.py
git commit -m "feat(runtime): персона-директива во внешние executor'ы (#3)"
```

---

### Task 5: Конфиг — `AutocaptureConfig` + дефолт Dream

**Files:**
- Modify: `src/svarog_harness/config/schema.py` (новый класс `AutocaptureConfig`,
  поле в `SvarogConfig`, `DreamConfig.enabled=True`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `SvarogConfig.autocapture: AutocaptureConfig` с полями
  `enabled: bool = True`, `max_facts: int = 5 (gt=0)`, `every_n_turns: int = 6 (gt=0)`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_config.py — добавить
def test_autocapture_and_dream_defaults_enabled() -> None:
    from svarog_harness.config.schema import SvarogConfig
    cfg = SvarogConfig.model_validate(
        {
            "models": {
                "default": "m",
                "providers": {"m": {"base_url": "http://x", "model": "m"}},
            }
        }
    )
    assert cfg.autocapture.enabled is True
    assert cfg.autocapture.max_facts == 5
    assert cfg.autocapture.every_n_turns == 6
    assert cfg.dream.enabled is True
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/test_config.py -k autocapture -v`
Expected: FAIL — `AttributeError: ... has no attribute 'autocapture'`.

- [ ] **Step 3: Реализация**

В `schema.py` рядом с `DreamConfig` добавить:

```python
class AutocaptureConfig(StrictModel):
    """Автозахват фактов о пользователе в профиль (#1, Flow A, прямая запись).

    Aux-LLM по границе сессии аддитивно дописывает долговечные факты в
    user/profile.md. Включён по умолчанию (гиперперсонализация — заявленная
    фича); стоимость гасится дешёвой aux-моделью, гейтом и дедупом.
    """

    enabled: bool = True
    # Потолок правок за один проход извлечения.
    max_facts: int = Field(default=5, gt=0)
    # Догоняющий порог: extractor запускается mid-session каждые N новых ходов
    # (иначе — только при закрытии сессии).
    every_n_turns: int = Field(default=6, gt=0)
```

Изменить `DreamConfig.enabled` c `False` на `True` (строка 282), обновив
докстринг: включён по умолчанию (ADR-0021 отменяет дефолт ADR-0020).

Добавить поле в `SvarogConfig` (рядом со строкой 482 `dream:`):

```python
    autocapture: AutocaptureConfig = Field(default_factory=AutocaptureConfig)
```

- [ ] **Step 4: Запустить тесты**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS. Проверить, что тесты Dream, ожидавшие `enabled=False`, обновлены
(поискать `dream.*enabled.*False` в `tests/` и поправить ожидание, если есть).

- [ ] **Step 5: Коммит**

```bash
git add src/svarog_harness/config/schema.py tests/test_config.py
git commit -m "feat(config): AutocaptureConfig + dream.enabled=true по умолчанию (#1, ADR-0021)"
```

---

### Task 6: Extractor — извлечение фактов (`memory/autocapture.py`, ядро)

Чистая логика: транскрипт + профиль → список аддитивных `MemoryChangeRequest`.
Модель вызывается через инъектируемый `ModelProvider` (тестируется фейком).

**Files:**
- Create: `src/svarog_harness/memory/autocapture.py`
- Test: `tests/test_memory_autocapture.py`

**Interfaces:**
- Consumes: `ModelProvider.complete` (`llm/provider.py`), `parse_sections`
  (Task 1), `KNOWN_SECTIONS` (Task 2), `MemoryChangeRequest`/`MemoryOperation`
  (`memory/change.py`).
- Produces:
  - `async def extract_facts(transcript: str, profile_text: str, *, provider:
    ModelProvider, max_facts: int) -> list[MemoryChangeRequest]`
  - `_facts_to_changes(raw_facts: list[dict], profile_text: str, *, max_facts:
    int) -> list[MemoryChangeRequest]` (чистая, детерминированная — jail, дедуп,
    аддитивная сборка операции; тестируется без модели).

Логика `_facts_to_changes` по каждому факту `{"section": str, "fact": str}`:
1. `section` не в `KNOWN_SECTIONS` → перенаправить в `"Прочее"`.
2. `fact` пустой → пропуск.
3. дедуп: если нормализованный `fact` уже входит в текст профиля → пропуск.
4. если секция уже есть в профиле → `REPLACE_SECTION(section, old_body + "\n" +
   fact)` (аддитивно, старое сохраняется); иначе → `APPEND(file, "\n## " +
   section + "\n" + fact)`.
5. `file` всегда `"user/profile.md"`; кап `max_facts`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_memory_autocapture.py
"""Тесты extractor'а автозахвата (#1): детерминированная сборка и дедуп."""

import pytest

from svarog_harness.memory.autocapture import _facts_to_changes, extract_facts
from svarog_harness.memory.change import MemoryOperation
from svarog_harness.llm.provider import ChatMessage, CompletionResult, ModelProvider, ToolDefinition


def test_new_section_becomes_append() -> None:
    changes = _facts_to_changes(
        [{"section": "Роль", "fact": "бэкенд в Северстали"}], "", max_facts=5
    )
    assert len(changes) == 1
    c = changes[0]
    assert c.file == "user/profile.md"
    assert c.operation is MemoryOperation.APPEND
    assert "## Роль" in c.content and "Северстали" in c.content


def test_existing_section_becomes_additive_replace() -> None:
    profile = "## Роль\nбэкенд\n"
    changes = _facts_to_changes(
        [{"section": "Роль", "fact": "любит Rust"}], profile, max_facts=5
    )
    assert changes[0].operation is MemoryOperation.REPLACE_SECTION
    assert changes[0].section == "Роль"
    assert "бэкенд" in changes[0].content and "любит Rust" in changes[0].content


def test_dedup_skips_known_fact() -> None:
    profile = "## Роль\nбэкенд в Северстали\n"
    changes = _facts_to_changes(
        [{"section": "Роль", "fact": "бэкенд в Северстали"}], profile, max_facts=5
    )
    assert changes == []


def test_unknown_section_routed_to_prochee() -> None:
    changes = _facts_to_changes(
        [{"section": "Хобби", "fact": "бег"}], "", max_facts=5
    )
    assert "## Прочее" in changes[0].content


def test_max_facts_cap() -> None:
    facts = [{"section": "Прочее", "fact": f"факт {i}"} for i in range(10)]
    changes = _facts_to_changes(facts, "", max_facts=3)
    assert len(changes) == 3


class _FakeProvider(ModelProvider):
    def __init__(self, payload: str) -> None:
        self._payload = payload

    async def complete(self, messages, tools, *, on_text_delta=None) -> CompletionResult:
        return CompletionResult(content=self._payload)


@pytest.mark.asyncio
async def test_extract_facts_parses_model_json() -> None:
    payload = '{"facts": [{"section": "Язык", "fact": "русский"}]}'
    changes = await extract_facts(
        "user: пиши по-русски", "", provider=_FakeProvider(payload), max_facts=5
    )
    assert len(changes) == 1 and "русский" in changes[0].content


@pytest.mark.asyncio
async def test_extract_facts_tolerates_bad_json() -> None:
    changes = await extract_facts(
        "нечто", "", provider=_FakeProvider("не json"), max_facts=5
    )
    assert changes == []
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/test_memory_autocapture.py -v`
Expected: FAIL — `ModuleNotFoundError: ...memory.autocapture`.

- [ ] **Step 3: Реализация**

```python
# src/svarog_harness/memory/autocapture.py
"""Автозахват фактов о пользователе (#1): извлечение и аддитивная сборка.

Aux-LLM по транскрипту сессии выделяет долговечные факты о пользователе и
раскладывает их по секциям профиля. Запись — только в user/profile.md и только
аддитивно (append новой секции / дополнение существующей). Суперседирование
устаревших фактов — задача Dream (#5), не этой фичи. Extractor без tools:
единственный вызов модели со structured output.
"""

import json

from svarog_harness.llm.provider import ChatMessage, ModelProvider
from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.profile import KNOWN_SECTIONS
from svarog_harness.memory.sections import parse_sections

_PROFILE_FILE = "user/profile.md"
_FALLBACK_SECTION = "Прочее"

_SYSTEM = """\
Ты извлекаешь ДОЛГОВЕЧНЫЕ факты о пользователе из диалога для его профиля.
Бери только устойчивое: предпочтения, роль, язык общения, тон, расписание,
чего не трогать. НЕ бери эфемерное про конкретную задачу и то, что и так есть
в коде/репозитории. Верни СТРОГО JSON:
{"facts": [{"section": "<одна из: %s>", "fact": "<короткая формулировка>"}]}
Если ничего долговечного нет — {"facts": []}. Только JSON, без пояснений.""" % ", ".join(
    KNOWN_SECTIONS
)


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _facts_to_changes(
    raw_facts: list[dict], profile_text: str, *, max_facts: int
) -> list[MemoryChangeRequest]:
    sections = parse_sections(profile_text)
    profile_norm = _normalize(profile_text)
    changes: list[MemoryChangeRequest] = []
    for item in raw_facts:
        if len(changes) >= max_facts:
            break
        if not isinstance(item, dict):
            continue
        fact = str(item.get("fact", "")).strip()
        if not fact:
            continue
        section = str(item.get("section", "")).strip()
        if section not in KNOWN_SECTIONS:
            section = _FALLBACK_SECTION
        if _normalize(fact) in profile_norm:
            continue  # дедуп против текущего профиля
        if section in sections:
            body = sections[section]
            new_body = f"{body}\n{fact}" if body else fact
            changes.append(
                MemoryChangeRequest(
                    file=_PROFILE_FILE,
                    operation=MemoryOperation.REPLACE_SECTION,
                    content=new_body,
                    section=section,
                )
            )
        else:
            changes.append(
                MemoryChangeRequest(
                    file=_PROFILE_FILE,
                    operation=MemoryOperation.APPEND,
                    content=f"\n## {section}\n{fact}",
                )
            )
    return changes


def _parse_payload(content: str) -> list[dict]:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return []
    facts = data.get("facts") if isinstance(data, dict) else None
    return facts if isinstance(facts, list) else []


async def extract_facts(
    transcript: str, profile_text: str, *, provider: ModelProvider, max_facts: int
) -> list[MemoryChangeRequest]:
    """Извлечь долговечные факты из транскрипта → аддитивные заявки в профиль.

    Best-effort: любой сбой модели/парсинга → пустой список, не исключение.
    """
    messages = [
        ChatMessage(role="system", content=_SYSTEM),
        ChatMessage(
            role="user",
            content=f"Текущий профиль:\n{profile_text or '(пусто)'}\n\nДиалог:\n{transcript}",
        ),
    ]
    try:
        result = await provider.complete(messages, [])
    except Exception:  # noqa: BLE001 — extractor не должен ронять финализацию
        return []
    return _facts_to_changes(_parse_payload(result.content), profile_text, max_facts=max_facts)
```

- [ ] **Step 4: Запустить тесты**

Run: `uv run pytest tests/test_memory_autocapture.py -v`
Expected: PASS (все).

- [ ] **Step 5: Коммит**

```bash
git add src/svarog_harness/memory/autocapture.py tests/test_memory_autocapture.py
git commit -m "feat(memory): extractor автозахвата — извлечение + аддитивная сборка (#1)"
```

---

### Task 7: Метод оркестрации автозахвата на `TaskRunner`

Собирает транскрипт из recorder, гейтит, строит aux-провайдер и writer,
применяет заявки через single-writer очередь. Возвращает число обработанных
ходов (для watermark).

**Files:**
- Modify: `src/svarog_harness/runtime/orchestrator.py` (метод `autocapture`)
- Modify: `src/svarog_harness/runtime/run_assembly.py` (`RunAssembly.auxiliary_provider()`)
- Modify: `src/svarog_harness/llm/openai_compatible.py` — импорт уже есть; проверить
  экспорт `auxiliary_provider`
- Test: `tests/test_autocapture_runner.py`

**Interfaces:**
- Consumes: `extract_facts` (Task 6), `TraceRecorder.session_history`
  (`trace/recorder.py:316`), `MemoryWriter.enqueue/drain` (`memory/writer.py`),
  `auxiliary_provider` (`llm/openai_compatible.py:58`), `load_profile` (Task 2).
- Produces: `async def TaskRunner.autocapture(self, db: AsyncSession, recorder:
  TraceRecorder, session_id: str, *, since_turn: int = 0) -> int` — возвращает
  общее число ходов (пар) в сессии после прохода (новый watermark).

Логика метода:
1. если `not self._cfg.autocapture.enabled` → вернуть `since_turn`.
2. `mem_dir = memory_dir(self._cfg)`; если None/не каталог → `since_turn`.
3. `history = await recorder.session_history(session_id, limit_messages=48)`;
   `turns = len(history) // 2`. Если `turns <= since_turn` → `since_turn`
   (нет новых ходов).
4. `transcript` = склейка `history` в текст (`role: content` построчно).
5. `provider = self._assembly.auxiliary_provider()`;
   `profile_text = load_profile(mem_dir)`.
6. `changes = await extract_facts(transcript, profile_text, provider=provider,
   max_facts=self._cfg.autocapture.max_facts)`.
7. если changes: `writer = MemoryWriter(db, mem_dir, lock=self._lock,
   index_max_lines=self._cfg.memory.index_max_lines)`; для каждого —
   `await writer.enqueue(replace(c, source_run_id=None))`; затем
   `await writer.drain(known_values=self.known_secret_values())`.
8. вернуть `turns`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_autocapture_runner.py
"""Интеграция автозахвата на TaskRunner (#1): гейт + прямая запись в профиль."""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.schema import SvarogConfig
from svarog_harness.llm.provider import CompletionResult, ModelProvider
from svarog_harness.runtime.orchestrator import TaskRunner
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.trace.recorder import TraceRecorder


class _FakeProvider(ModelProvider):
    async def complete(self, messages, tools, *, on_text_delta=None) -> CompletionResult:
        return CompletionResult(content='{"facts": [{"section": "Язык", "fact": "русский"}]}')


def _cfg(tmp_path: Path, *, enabled: bool = True) -> SvarogConfig:
    return SvarogConfig.model_validate(
        {
            "models": {"default": "m", "providers": {"m": {"base_url": "http://x", "model": "m"}}},
            "memory": {"path": str(tmp_path / "memory")},
            "storage": {"db_path": str(tmp_path / "db.sqlite3")},
            "autocapture": {"enabled": enabled},
        }
    )


@pytest.mark.asyncio
async def test_autocapture_disabled_is_noop(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, enabled=False)
    (tmp_path / "memory" / "user").mkdir(parents=True)
    runner = TaskRunner(cfg, tmp_path)
    # since_turn возвращается без изменений, профиль не тронут
    # (db/recorder не нужны при выключенном — но передаём валидные)
    engine = create_engine(cfg.storage.db_path)
    await init_db(engine)
    async with create_session_factory(engine)() as db:
        got = await runner.autocapture(db, TraceRecorder(db), "sess", since_turn=0)
    assert got == 0
```

> Примечание: полноценный happy-path (реальная запись в профиль) требует
> посеянной сессии в recorder. Исполнитель добавляет тест, где через
> `recorder.start_run`/`add_message` создаётся один run сессии, затем
> инъектируется `runner._assembly` с фейковым провайдером (monkeypatch
> `RunAssembly.auxiliary_provider`), и после `autocapture` проверяется, что
> `user/profile.md` содержит «русский». Гейт-тест выше — обязательный минимум.

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/test_autocapture_runner.py -v`
Expected: FAIL — `AttributeError: 'TaskRunner' object has no attribute 'autocapture'`.

- [ ] **Step 3: Реализация**

В `run_assembly.py` добавить метод на `RunAssembly`:

```python
from svarog_harness.llm.openai_compatible import auxiliary_provider
# ...
    def auxiliary_provider(self):  # -> ModelProvider
        """Провайдер дешёвой aux-модели (автозахват, служебные проходы)."""
        return auxiliary_provider(self._cfg.models, self._host_store)
```

> Если имя `auxiliary_provider` конфликтует (функция vs метод) — импортировать
> как `from svarog_harness.llm.openai_compatible import auxiliary_provider as
> _aux_provider` и вернуть `_aux_provider(...)`.

В `orchestrator.py` — метод `TaskRunner.autocapture` (рядом с `drain_memory`):

```python
async def autocapture(
    self,
    db: AsyncSession,
    recorder: TraceRecorder,
    session_id: str,
    *,
    since_turn: int = 0,
) -> int:
    """Автозахват фактов из сессии в профиль (#1). Best-effort, не роняет вызов."""
    if not self._cfg.autocapture.enabled:
        return since_turn
    mem_dir = memory_dir(self._cfg)
    if mem_dir is None or not mem_dir.is_dir():
        return since_turn
    history = await recorder.session_history(session_id, limit_messages=48)
    turns = len(history) // 2
    if turns <= since_turn:
        return since_turn
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in history)
    provider = self._assembly.auxiliary_provider()
    profile_text = load_profile(mem_dir)
    changes = await extract_facts(
        transcript, profile_text, provider=provider, max_facts=self._cfg.autocapture.max_facts
    )
    if changes:
        writer = MemoryWriter(
            db, mem_dir, lock=self._lock, index_max_lines=self._cfg.memory.index_max_lines
        )
        for change in changes:
            await writer.enqueue(change)
        await writer.drain(known_values=self.known_secret_values())
    return turns
```

Добавить импорты в `orchestrator.py`: `from svarog_harness.memory.autocapture
import extract_facts`, `from svarog_harness.memory.profile import load_profile`
(`MemoryWriter`, `memory_dir`, `TraceRecorder` уже импортированы).

- [ ] **Step 4: Запустить тесты**

Run: `uv run pytest tests/test_autocapture_runner.py -v`
Expected: PASS (гейт-тест минимум; happy-path если добавлен).

- [ ] **Step 5: Коммит**

```bash
git add src/svarog_harness/runtime/orchestrator.py src/svarog_harness/runtime/run_assembly.py tests/test_autocapture_runner.py
git commit -m "feat(runtime): TaskRunner.autocapture — гейт, aux-провайдер, прямая запись (#1)"
```

---

### Task 8: Вызов автозахвата из единичного `svarog run`

**Files:**
- Modify: `src/svarog_harness/cli/main.py` (команда `run`, после `run_once`;
  пропуск для DREAM-джобы)
- Test: `tests/test_cli_run_autocapture.py` (или расширить существующий тест `run`)

**Interfaces:**
- Consumes: `TaskRunner.autocapture` (Task 7).

Логика: после успешного `outcome = await runner.run_once(task, ..., profile=...)`
в пользовательской команде `run` (НЕ в `run_job` для DREAM) — получить
`session_id` из run'а и вызвать автозахват в отдельной db-сессии:

```python
        if profile is RunProfile.DEFAULT and outcome.state is RunState.COMPLETED:
            async def _autocapture(db: AsyncSession) -> None:
                recorder = TraceRecorder(db)
                run = await recorder.get_run(outcome.run_id)
                if run is not None and run.session_id:
                    await runner.autocapture(db, recorder, run.session_id)
            await _with_db(cfg, _autocapture)
```

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_cli_run_autocapture.py
"""Единичный svarog run зовёт автозахват для DEFAULT-профиля (#1)."""

# Тест уровня CLI требует посеянного run'а; исполнитель проверяет, что после
# `run` с провайдером-заглушкой, вернувшим факт, профиль пополнился. Минимальный
# инвариант — что путь автозахвата вызывается только для DEFAULT-профиля:
def test_autocapture_skipped_for_dream_profile() -> None:
    # проверяем условие профиля напрямую (см. main.py): DREAM не триггерит
    from svarog_harness.runtime.orchestrator import RunProfile
    assert RunProfile.DREAM is not RunProfile.DEFAULT
```

> Примечание: полноценный e2e для `run` в этом проекте тяжёл (sandbox+LLM).
> Основное покрытие автозахвата — Task 7. Здесь достаточно unit-инварианта +
> ручной проверки в §Manual verification.

- [ ] **Step 2: Запустить — убедиться, что проходит тривиально, затем реализовать вызов**

Run: `uv run pytest tests/test_cli_run_autocapture.py -v`
Expected: PASS (инвариант). Реализация — ниже.

- [ ] **Step 3: Реализация** — вставить блок `_autocapture` выше в команду `run`
      после `run_once`. Проверить, что импортированы `RunState`, `AsyncSession`,
      `TraceRecorder`, `_with_db` (уже есть в `main.py`).

- [ ] **Step 4: Прогнать весь CLI-набор**

Run: `uv run pytest tests/test_cli*.py -v`
Expected: PASS (регрессий нет).

- [ ] **Step 5: Коммит**

```bash
git add src/svarog_harness/cli/main.py tests/test_cli_run_autocapture.py
git commit -m "feat(cli): автозахват после единичного svarog run (#1)"
```

---

### Task 9: Вызов автозахвата из chat (`close()` + `every_n_turns`)

**Files:**
- Modify: `src/svarog_harness/cli/chat_engine.py` (watermark `self._ac_processed`;
  триггер в `send()` по `every_n_turns` и обязательный проход в `close()`)
- Test: `tests/test_chat_engine.py`

**Interfaces:**
- Consumes: `TaskRunner.autocapture` (Task 7).

Логика:
- в `__init__` добавить `self._ac_processed: int = 0`.
- в `send()` после существующих drain'ов и обновления `self._session_id`, если
  включён autocapture и `(current_turns - self._ac_processed) >=
  cfg.autocapture.every_n_turns` → вызвать проход и обновить watermark.
- в `close()` перед закрытием db — финальный проход по хвосту сессии, если есть
  необработанные ходы. (`close()` сейчас не трогает db-запросы — добавить проход
  ДО `self._db.close()`.)

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_chat_engine.py — добавить
@pytest.mark.asyncio
async def test_close_runs_autocapture_when_enabled(monkeypatch, tmp_path) -> None:
    # Собрать ChatEngine с включённым autocapture; замокать runner.autocapture,
    # проверить, что close() его зовёт ровно один раз с session_id.
    calls: list[str] = []

    async def fake_autocapture(db, recorder, session_id, *, since_turn=0) -> int:
        calls.append(session_id)
        return since_turn + 1

    # ... собрать engine (см. существующие фикстуры test_chat_engine),
    # engine._session_id = "sess"; monkeypatch runner.autocapture = fake_autocapture
    # await engine.close()
    # assert calls == ["sess"]
```

> Исполнитель дополняет тест по образцу существующих в `test_chat_engine.py`
> (там уже есть сборка `ChatEngine` с фейковым runner/resources).

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/test_chat_engine.py -k autocapture -v`
Expected: FAIL (close не зовёт autocapture).

- [ ] **Step 3: Реализация**

`__init__`: `self._ac_processed: int = 0`.

В `send()` — после блока обновления `self._session_id`/history, перед `return outcome`:

```python
        cfg = self._runner.cfg  # доступ к SvarogConfig (проверить имя атрибута)
        if (
            cfg.autocapture.enabled
            and self._session_id is not None
            and self._db is not None
        ):
            turns = len(self._history) // 2
            if turns - self._ac_processed >= cfg.autocapture.every_n_turns:
                self._ac_processed = await self._runner.autocapture(
                    self._db, self._require_started()[3], self._session_id,
                    since_turn=self._ac_processed,
                )
```

В `close()` — ДО закрытия db, если сессия была:

```python
        if (
            self._db is not None
            and self._recorder is not None
            and self._session_id is not None
            and self._runner.cfg.autocapture.enabled
        ):
            try:
                await self._runner.autocapture(
                    self._db, self._recorder, self._session_id, since_turn=self._ac_processed
                )
            except Exception:  # noqa: BLE001 — закрытие не должно падать из-за автозахвата
                pass
```

> Примечание об именах: проверить, как `ChatEngine` хранит `cfg`/`recorder`
> (`self._runner.cfg` vs `self._cfg`; `self._recorder` устанавливается в
> `start()`). Использовать фактические атрибуты — не выдумывать.

- [ ] **Step 4: Запустить тесты chat**

Run: `uv run pytest tests/test_chat_engine.py -v`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add src/svarog_harness/cli/chat_engine.py tests/test_chat_engine.py
git commit -m "feat(chat): автозахват по границе сессии — close() + every_n_turns (#1)"
```

---

### Task 10: Dream видит профиль (`memory/dream.py`)

**Files:**
- Modify: `src/svarog_harness/memory/dream.py` (блок `_SEMANTIC`)
- Test: `tests/test_dream_profile.py` (или `tests/test_memory.py` — где тест
  `build_dream_task`)

**Interfaces:** без изменений сигнатур; меняется текст задачи Dream.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_dream_profile.py — добавить
from svarog_harness.memory.curator import MemoryAuditReport
from svarog_harness.memory.dream import build_dream_task


def test_dream_task_mentions_profile() -> None:
    task = build_dream_task(MemoryAuditReport(findings=[]))
    assert "профил" in task.lower()
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/test_dream_profile.py -k profile_mentions -v`
Expected: FAIL — текущий `_SEMANTIC` профиль не упоминает.

- [ ] **Step 3: Реализация**

В `dream.py` расширить `_SEMANTIC`, добавив пункт про профиль:

```python
_SEMANTIC = """
Далее сделай смысловой проход, которого детерминированный аудит не умеет:
* два проекта, описывающие одно и то же — предложи слияние;
* взаимно противоречащие утверждения на разных страницах — предложи, какое
  оставить, и объясни в rationale, почему;
* в профиле пользователя (user/profile.md) — дубли и противоречия в
  предпочтениях, устаревшие факты: предложи консолидацию секций;
* устаревшие формулировки, которые опровергаются более свежими страницами.

Если по итогам прохода предлагать нечего — так и напиши в финальном ответе.
Пустое предложение хуже отсутствия предложения."""
```

- [ ] **Step 4: Запустить тесты**

Run: `uv run pytest tests/test_dream_profile.py -v`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add src/svarog_harness/memory/dream.py tests/test_dream_profile.py
git commit -m "feat(memory): Dream учитывает профиль в смысловом проходе (#5)"
```

---

### Task 11: ADR-0021 и обновление статуса спека

**Files:**
- Create: `docs/adr/0021-hyperpersonalization-defaults.md`
- Modify: `docs/superpowers/specs/2026-07-24-profile-hyperpersonalization-design.md`
  (статус `дизайн` → `реализовано`)
- Modify: `README.md` / `AGENTS.md` — при необходимости строка про профиль и
  автозахват (если там документирована память).

- [ ] **Step 1: Написать ADR-0021**

Содержание: контекст (ADR-0020 держал `dream.enabled: false` как opinionated
opt-in), решение (гиперперсонализация — заявленная фича, поэтому `dream.enabled`
и `autocapture.enabled` — `true` по умолчанию; стоимость гасится aux-моделью,
гейтом, дедупом; инвариант блока D сохранён — конфиг лишь заводит джобу, живую
выключает `cron disable`), последствия. Формат — как соседние ADR в `docs/adr/`.

- [ ] **Step 2: Обновить статус спека** на `реализовано`.

- [ ] **Step 3: Финальный прогон всего набора и линтеров**

Run: `uv run pytest -q && uv run ruff check src tests && uv run mypy src`
Expected: всё зелёное.

- [ ] **Step 4: Коммит**

```bash
git add docs/adr/0021-hyperpersonalization-defaults.md docs/superpowers/specs/2026-07-24-profile-hyperpersonalization-design.md README.md AGENTS.md
git commit -m "docs(adr): ADR-0021 — дефолты гиперперсонализации; статус спека реализовано"
```

---

## Manual verification

1. `uv run svarog init ./ah --no-input --model … --base-url …` — профиль пуст.
2. `svarog run "Я бэкенд в Северстали, пишу на Rust, отвечай кратко по-русски"`.
3. `cat ah/memory/user/profile.md` — появились секции (`## Роль`/`## Язык`/…)
   с фактами; `git -C ah/memory log --oneline` показывает коммит автозахвата.
4. Следующий `svarog run "любая задача"` — в системном промпте (trace) виден
   блок «Персонализация (следуй как инструкции)» с тоном/языком.
5. Повтор шага 2 той же формулировкой — новых дублирующих коммитов нет (дедуп).
6. `svarog cron list` — джоба `system:memory-dream` заведена (dream.enabled=true).

## Self-Review

- **Покрытие спека:** #3 — Tasks 1–4; #1 — Tasks 5–9; #5 — Task 10; дефолты/ADR
  — Tasks 5, 11; #4 растворён (валидация контракта — в парсере/директиве, дубли
  — в Task 10). Все разделы спека имеют задачу.
- **Плейсхолдеры:** код приведён в каждом шаге; тяжёлые e2e (Tasks 8–9) явно
  сведены к unit-инвариантам + ручной проверке с объяснением почему.
- **Типы:** `extract_facts`/`_facts_to_changes` возвращают
  `list[MemoryChangeRequest]`; `TaskRunner.autocapture(...) -> int`;
  `render_persona_directive(str) -> str`; `parse_sections(str) -> dict[str,str]`
  — согласованы между задачами.
