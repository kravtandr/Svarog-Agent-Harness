# Анализ референсных проектов: hermes-agent и HKUDS/OpenHarness

Оба репозитория склонированы в `/home/kravtandr/reference/` (shallow clone, 2026-07-08). Оба под **MIT** — код можно переиспользовать напрямую с сохранением копирайта.

## 1. NousResearch/hermes-agent

Зрелый production-агент (~сотни модулей): TUI, gateway на 6 платформ (Telegram, Discord, Slack, WhatsApp, Signal, CLI), 6 execution-окружений, curator, компакция контекста, cron-планировщик, subagents, batch trajectory generation. Python, монолитный процесс.

### 1.1. Curator — главное, что нас интересует

`agent/curator.py` (~2000 строк) + `tools/skill_usage.py`. Устройство подтверждает нашу двухслойную модель и добавляет проверенные практикой детали:

**Слой 1 — детерминированный** (`apply_automatic_transitions`): lifecycle `active → stale → archived` по timestamp последней активности (stale после 30 дней, archive после 90, конфигурируемо). Применяется автоматически, без LLM и без approval.

**Слой 2 — LLM-консолидация** (`run_curator_review`): форк агента на **auxiliary-модели** (дешевой, отдельной от основной сессии — не портит prompt cache) может pin/archive/consolidate/patch скиллы. **Выключен по умолчанию** (opt-in) — консолидация «опинионейтед» и стоит денег.

**Инварианты, которые стоит перенять дословно:**

* curator трогает **только agent-created** скиллы (provenance-маркировка при создании через skill_manage); bundled и вручную написанные — вне зоны действия;
* **никогда не удаляет** — только архивирует в `.archive/` (обратимо);
* `pinned` — флаг, выводящий скилл из-под любых авто-переходов;
* скиллы, на которые ссылается **любой cron job** (включая paused), считаются используемыми — иначе редко срабатывающая автоматизация теряет свой скилл;
* новый скилл получает якорь `created_at`, чтобы не заархивироваться сразу;
* телеметрия использования — в **sidecar-файле** (`.usage.json`), не во frontmatter SKILL.md: операционные данные не загрязняют человекочитаемый контент и не создают конфликтов;
* запуск — **inactivity-triggered** (агент idle + прошло `interval_hours`), без отдельного cron-демона;
* каждый прогон пишет markdown-отчет.

### 1.2. Approval (`tools/approval.py`)

* `DANGEROUS_PATTERNS` — готовый выверенный набор паттернов опасных bash-команд → прямой донор для наших bash-эвристик (слой 2 ADR-0002);
* **YOLO-режим замораживается при старте процесса**: env-переменная читается один раз при импорте, иначе скилл мог бы выставить её изнутри и снять все проверки — классическая prompt-injection-эскалация. Принцип обязателен и нам;
* smart approval: aux-LLM авто-подтверждает низкорисковые команды;
* постоянный allowlist в конфиге.

### 1.3. Execution environments (`tools/environments/`)

Единая модель «spawn-per-call»: каждая команда — свежий `bash -c`, снапшот сессии (env, функции, aliases) снимается при init и восстанавливается перед командой, cwd переживает вызовы. Backends: `local`, `docker`, `ssh`, `singularity`, `modal`, `daytona` за одним базовым классом. Это готовая реализация нашего интерфейса `SandboxBackend` — минимум `docker.py` + `local.py` стоит адаптировать, а не писать с нуля.

### 1.4. Компакция контекста (`agent/context_compressor.py`)

Aux-модель суммирует середину разговора, защищая голову и хвост (по токен-бюджету, не по числу сообщений); структурированный шаблон summary (Resolved/Pending); дешевый pre-pass обрезки tool outputs перед LLM; итеративное обновление summary между компакциями; секции «historical/reference-only», чтобы старое summary не читалось как активные инструкции. Готовый дизайн для нашей пост-MVP компакции.

### 1.5. Прочее ценное

* **Auxiliary model** как отдельная сущность конфигурации: дешевая модель для компакции, curator, smart approval, суммаризации сессий;
* FTS5-поиск по прошлым сессиям + LLM-суммаризация для cross-session recall;
* «nudges» — периодические напоминания агенту сохранять знания в память/скиллы;
* `MemoryManager` — provider-модель памяти (prefetch перед ходом / sync после хода);
* checkpoint_manager, iteration_budget, delegate_tool (изолированные subagents);
* code execution RPC: агент пишет Python-скрипт, вызывающий tools, — многошаговый pipeline за один ход без расхода контекста;
* совместимость SKILL.md с открытым стандартом [agentskills.io](https://agentskills.io).

## 2. HKUDS/OpenHarness

Молодой проект (v0.1.x): «oh» — lightweight harness (engine, 43 tools, skills, memory, permissions, MCP, swarm/coordinator) + «ohmo» — персональный агент поверх него. Python ядро + React Ink TUI. Работает поверх подписок Claude Code/Codex (bridge), а не только напрямую с API.

Ценное:

* **`skills/_frontmatter.py` + `loader.py`** — маленький чистый парсер SKILL.md, совместимый с тем же стандартом; `SkillDefinition` с полезными полями: `user_invocable`, `disable_model_invocation`, `aliases`, `argument_hint` (скилл = одновременно slash-команда для человека);
* **permission modes**: `default / plan / full_auto` — подтверждает наш дизайн профилей автономии; **plan mode** (агент строит план без исполнения) — режим, которого у нас нет;
* **memdir**: project-scoped память с entrypoint `MEMORY.md`, ограниченным по байтам/строкам, и policy-строками — деталь «лимит на entrypoint» стоит взять;
* `query_engine` (~300 строк) — пример компактного agent loop: streaming, retry с backoff, параллельные tool calls, cost tracking.

Осознанно **не** берем: авторизацию поверх чужих подписок (bridge к Claude Code/Codex) — у нас model-agnostic API-подход; React Ink TUI (у нас Typer/Rich, TUI — не MVP).

## 3. Что взять готовым (код, MIT)

| Что | Откуда | Куда у нас |
|---|---|---|
| Execution environments (`base.py`, `docker.py`, `local.py`, позже `ssh.py`) | hermes `tools/environments/` | `sandbox/` — реализация SandboxBackend (issue 10) |
| `DANGEROUS_PATTERNS` + детектор | hermes `tools/approval.py` | `policy/bash_heuristics.py` (issue 11) |
| Frontmatter-парсер SKILL.md | HKUDS `skills/_frontmatter.py` | `skills/loader.py` (issue 14) |
| Шаблон структурированного summary + head/tail-защита | hermes `context_compressor.py` | пост-MVP компакция; частично — refuel `task_state.md` (issue 18) |
| Схема curator-отчета и инварианты | hermes `curator.py` | `skills/curator/` (issues 27–28) |

Важно: адаптация, не вендоринг — их код завязан на свои конфиги/логгеры; берем логику и паттерны, сохраняя MIT-атрибуцию в NOTICE.

## 4. Концепции перенять (обновления наших документов)

1. **Auxiliary model** в конфигурации (`models.auxiliary`) — для curator, компакции, verifier-судьи. Дешевая модель для служебных LLM-задач. → ТЗ §13, §6.11.
2. **Режим автономии фиксируется при старте run** и не перечитывается из env/конфига во время исполнения — иначе prompt injection может эскалировать до yolo. → ТЗ §12, ADR-0010.
3. **Curator-инварианты Hermes** → ADR-0009/§18.1: только agent-created скиллы, pinned-флаг, защита скиллов из scheduled-задач, sidecar/SQLite-телеметрия (у нас уже SQLite trace — наш вариант сильнее), якорь created_at.
4. **Слой 1 curator применяется автоматически** (без proposals): переходы active→stale→archived обратимы и касаются только agent-created скиллов — гонять их через governance flow избыточно и противоречит yolo-first. Consolidation (слой 2) — по-прежнему только через proposals. → правка ADR-0009.
5. **Совместимость SKILL.md с agentskills.io** — бесплатная экосистема готовых скиллов. → ТЗ §7.
6. **Plan mode** (из HKUDS) — профиль «построй план, не исполняй» как четвертый режим рядом с supervised/auto/yolo. → ТЗ §3.6 (пост-MVP).
7. **Лимит байт на memory entrypoint** (из HKUDS memdir) — защита контекста от разрастания памяти. → §6.3/6.7.

## 5. Что мы делаем лучше (осознанные отличия — не менять)

* **Git-native память** с историей, rollback и single-writer: у Hermes память — файлы + sidecar JSON без версионирования, у HKUDS — memdir без Git. Наше главное отличие.
* **Sandbox по умолчанию + enforcement-first** (ADR-0002): у Hermes по умолчанию local-исполнение, безопасность — паттерны+approval; docker — опция. У нас гарантии не зависят от распознавания команд.
* **Typed critical-набор + Policy Engine как отдельный слой**: у обоих референсов политика размазана по коду.
* **Resumable runs как state machine с write-ahead checkpoint** (ADR-0005): у Hermes есть checkpoint_manager, но run не переживает процесс как первоклассная сущность.
* **Полный SQLite-trace каждого действия** и secret scan перед каждым коммитом (у референсов отсутствует — у них нет git-flows как концепции).
* **Skill governance через git proposals** для содержательных изменений скиллов.

## 6. Оценка трудозатрат, которые экономим

Адаптация environments (докер+local) ≈ экономит issue 10 почти целиком; DANGEROUS_PATTERNS ≈ половина issue 11-эвристик; frontmatter-парсер ≈ треть issue 14; дизайн компакции и curator — снимает проектные риски M5. Суммарно — недели работы и, что важнее, чужие «шишки» уже учтены (cron-referenced skills, YOLO-freeze, sidecar-телеметрия, protected head/tail при компакции).
