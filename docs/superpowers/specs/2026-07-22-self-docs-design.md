# Self-docs для external executor'ов

**Дата:** 2026-07-22
**Статус:** design (approved)
**Скоуп:** внешние executor'ы (Claude Code, OpenCode, Codex). Нативный `AgentLoop` — вне скоупа.

## Проблема

Св_арог не умеет отвечать на вопросы пользователя о себе самом — командах,
фичах, архитектурных решениях. При основном сценарии использования (внешний
executor в sandbox) агент не имеет доступа к документации проекта: она лежит на
хосте (`README.md`, `AGENTS.md`, `docs/adr/`), а контейнер видит только
`/workspace` (текущий проект пользователя) и launch-файлы.

Задача: дать внешнему агенту доступ к документации Св_арога, чтобы на вопрос
«как в свароге сделать X?» или «что такое refuel loop?» он отвечал по реальной
доке, а не выдумывал.

## Ключевое ограничение

MCP reverse-tools (bridge `/svarog/mcp`) доходят **только до Claude Code** —
в матрице `AdapterCapabilities` флаг `mcp` есть лишь у `claude-code`;
у `opencode`/`codex` — `resume` без `hooks`/`mcp`
(`runtime/agents/__init__.py`). Значит MCP-tool покрыл бы один executor из трёх.

**Файлы, смонтированные в контейнер**, доступны любому executor'у через его
нативные `Read`/`Grep`. Поэтому основной механизм — файловый, а не MCP.

## Принятые решения (из брейншторма)

| Вопрос | Решение |
|---|---|
| Объём доки | usage/how-to + архитектура (ADR) |
| Покрытие | все внешние executor'ы (не нативный loop) |
| Доставка | mount host-side `:ro` при старте контейнера |
| Источник | сырые доки репо (`README.md`, `AGENTS.md`, `docs/adr/`) + генерируемый `INDEX.md` |
| Форма источника | стейдж-копия (не прямой mount `docs/` из репо) |
| Путь в контейнере | `/opt/svarog-docs` (ro) |
| Тумблер | `executor.external.self_docs: true` (default on, opt-out) |

## Отвергнутые альтернативы

- **MCP reverse-tool `read_svarog_docs` на bridge.** Дошёл бы только до Claude
  Code. Один файловый механизм для всех лучше двух параллельных. YAGNI.
- **Прямой mount `docs/` из репо.** Тащит в контейнер лишнее (`docs/superpowers/`,
  `reference-analysis.md` и т.д.), не даёт места под `INDEX.md`, хрупок к
  упаковке. Стейдж-копия даёт кураторский контроль.
- **RAG/эмбеддинги.** Плоские markdown + grep достаточно; индекс/векторка —
  оверинжиниринг для объёма в сотни КБ.
- **Нативный loop.** Отдельный механизм (tool/скилл на хосте) — вне скоупа
  текущей задачи.

## Архитектура

### Общая механика

При старте контейнера `ExternalAgentInfra.prepare_launch()`:

1. **Стейджит** копию документации в launch-директорию run'а
   (рядом с `mcp.json`/`hook.py`).
2. **Монтирует** её `:ro` в контейнер по пути `/opt/svarog-docs` через
   существующий механизм `extra_mounts`.
3. **Встраивает указатель** на этот путь в контекст-файл агента
   (`CLAUDE.md` / `AGENTS.md` / opencode-конфиг) через `context_files()`.

Ноль нового рантайм-протокола. Переиспользуются те же рельсы, что и для
`mcp.json`, `hook.py` и карточек скиллов.

### Компонент 1: резолвер и стейджинг — `runtime/self_docs.py` (новый модуль)

**`resolve_docs_root() -> Path | None`**
Находит корень репо/пакета: от `Path(__file__)` вверх до директории, где есть
и `README.md`, и `docs/adr/`. Если не найдено (нестандартная упаковка без
docs) — возвращает `None`; фича мягко отключается, run не падает.

**`stage_self_docs(dest: Path) -> Path | None`**
- Если `resolve_docs_root()` вернул `None` → возвращает `None` (no-op).
- Иначе копирует в `dest`:
  - `README.md` → `dest/README.md`
  - `AGENTS.md` → `dest/AGENTS.md` (если существует)
  - `docs/adr/*.md` → `dest/adr/*.md`
- Генерит `dest/INDEX.md` (см. Компонент 2).
- Возвращает `dest`.
- Отсутствие отдельного файла (напр. `AGENTS.md`) — не ошибка, просто
  пропускается и не попадает в `INDEX.md`.

### Компонент 2: `INDEX.md` (навигация, progressive disclosure)

Генерируемое оглавление, чтобы агент читал прицельно, а не тянул сотни КБ в
контекст. Структура:

```
# Svarog — документация

## Использование
- README.md — команды, флаги, фичи, quick start

## Правила репозитория
- AGENTS.md — как работать с кодовой базой Svarog

## Архитектурные решения (ADR)
- adr/0001-<slug>.md — <заголовок из первой строки>
- adr/0002-<slug>.md — <заголовок>
  ...
```

Заголовки ADR парсятся из первой markdown-заголовочной строки каждого
`docs/adr/*.md` (напр. `# ADR-0016. External Agent Executor`). Файлы, которых
не оказалось при стейджинге, в `INDEX.md` не попадают.

### Компонент 3: mount — хелпер `_add_launch_dir`

В `ExternalAgentInfra` — новый хелпер по аналогии с `_add_launch_file`, но
монтирует **директорию**, а не одиночный файл:

```
_add_launch_dir(name: str, container_path: str) -> str | None
```

- Создаёт `self._launch_dir / name`, вызывает `stage_self_docs()` в неё.
- Если стейджинг вернул `None` → возвращает `None`, mount не добавляется.
- В docker-режиме добавляет `(host_dir, container_path, True)` в `_extra_mounts`
  и возвращает `container_path`.
- В local-trusted режиме (только тесты) возвращает хостовый путь.

Вызывается из `prepare_launch()` при `self_docs=True`.

### Компонент 4: discovery-wiring через `context_files()`

Чтобы агент **знал** о существовании доков и лез туда при вопросах о Св_ароге,
в контекст-файл каждого адаптера добавляется короткий блок. Текст — общая
константа (напр. `SELF_DOCS_HINT` в `runtime/self_docs.py` или в `executor.py`):

```
## Документация Svarog
На вопросы про сам Svarog (команды, фичи, архитектура) отвечай по документации
в /opt/svarog-docs. Сначала прочитай /opt/svarog-docs/INDEX.md, затем нужный
файл. Не выдумывай поведение Svarog — читай доку.
```

Каждый адаптер (`claude_code.py`, `opencode.py`, `codex.py`) вставляет этот
блок в свой контекст-файл в `context_files()` — рядом с памятью и карточками
скиллов. Блок добавляется только когда self-docs включён и путь смонтирован
(иначе указывал бы на несуществующую директорию — «ложь агенту», как с
ask_user-преамбулой у codex).

Механика передачи флага/пути в `context_files()`: сигнатура `context_files()`
расширяется опциональным параметром (напр. `self_docs_path: str | None`), либо
`prepare_launch` дописывает блок в собранный `state_files` централизованно.
Конкретный способ выбирается на этапе плана — предпочтителен тот, что меньше
трогает сигнатуры адаптеров.

### Компонент 5: конфиг-тумблер

В схему `executor.external` добавляется поле:

```yaml
executor:
  external:
    self_docs: true   # default on; false — не стейджить и не монтировать доки
```

При `self_docs=False` — `prepare_launch` пропускает стейджинг, mount и
discovery-блок.

## Поток данных

```
prepare_launch()
  ├─ if self_docs:
  │    docs_path = _add_launch_dir("docs", "/opt/svarog-docs")
  │       └─ stage_self_docs(launch_dir/docs)
  │            ├─ resolve_docs_root()  → repo root | None
  │            ├─ copy README.md, AGENTS.md, adr/*.md
  │            └─ generate INDEX.md
  │       └─ extra_mounts += (host_dir, /opt/svarog-docs, ro)
  │    if docs_path: inject SELF_DOCS_HINT into context files
  └─ ... (mcp.json, hook.py, managed-settings как раньше)

container start
  └─ agent видит /opt/svarog-docs/{INDEX.md,README.md,AGENTS.md,adr/*.md} :ro
  └─ на вопрос о Svarog: Read INDEX.md → Grep/Read нужного файла
```

## Обработка ошибок и деградация

- **docs-root не найден** → `stage_self_docs` возвращает `None` → mount не
  добавляется, discovery-блок не пишется. Run работает как раньше. Логируется
  предупреждение (self-docs недоступны).
- **Отдельный файл отсутствует** (`AGENTS.md`) → пропускается, run продолжается.
- **`self_docs=False`** → вся ветка пропускается.
- Директория монтируется `:ro`, non-root, вне state volume — агент не может её
  переписать. Никакого egress. Доки публичны (README/ADR) — секретов нет.

## Границы (non-goals)

- Нативный `AgentLoop` не покрывается.
- Не монтируется `TASK.md` (77КБ ТЗ), `docs/superpowers/`, `simulation/`,
  `docs/reference-analysis.md` — шум.
- Нет индексации/эмбеддингов/RAG — плоские файлы + нативный grep агента.
- Нет MCP reverse-tool для доков.

## Тестирование

- `resolve_docs_root()`: находит корень из пути пакета; возвращает `None`, если
  маркеров нет.
- `stage_self_docs()`: копирует ожидаемый набор; генерит `INDEX.md` с
  корректными ADR-заголовками; корректно работает при отсутствии `AGENTS.md`;
  возвращает `None` при `resolve_docs_root()=None`.
- `INDEX.md`: парсинг заголовка ADR из первой заголовочной строки.
- `prepare_launch` с `self_docs=True`: появляется dir-mount на `/opt/svarog-docs`
  и discovery-блок в контекст-файле; с `self_docs=False` — ни того, ни другого.
- Деградация: при недоступном docs-root run стартует без доков и без падения.

## Точки в существующем коде

- `src/svarog_harness/runtime/self_docs.py` — **новый**: резолвер, стейджинг,
  генерация INDEX, константа `SELF_DOCS_HINT`.
- `src/svarog_harness/runtime/agent_infra.py` — `_add_launch_dir`, вызов из
  `prepare_launch`, проброс пути в discovery-wiring.
- `src/svarog_harness/runtime/agents/{claude_code,opencode,codex}.py` —
  вставка `SELF_DOCS_HINT` в `context_files()`.
- `src/svarog_harness/runtime/executor.py` — возможное расширение сигнатуры
  `context_files()` (если выбран этот способ проброса).
- `src/svarog_harness/config/schema.py` — поле `executor.external.self_docs`.
