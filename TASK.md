Ниже — цельное ТЗ для **Svarog** без деления на этапы.

# Техническое задание: Svarog Agent Harness

## 0. Именование

* Полное имя проекта / GitHub-репозиторий: **Svarog-Agent-Harness**
* Короткое имя в тексте и коде: **Svarog**
* PyPI-пакет: **svarog-harness** (имя `svarog` на PyPI занято сторонней библиотекой)
* Python-модуль: **svarog_harness** (не `svarog` — чтобы не конфликтовать с существующим PyPI-пакетом при совместной установке)
* CLI-команда: **svarog**

## 1. Концепция проекта

**Svarog** — это open-source платформа для создания self-hosted ИИ-агентов нового поколения, построенных вокруг концепции **agent harness**, **Git-native памяти**, **скиллов**, **sandboxed execution** и **долгоживущих агентных циклов**.

Svarog не является классическим чат-ботом, workflow-конструктором или графовым фреймворком. Его задача — предоставить безопасную, расширяемую и воспроизводимую среду исполнения, в которой LLM-агент может работать с файлами, shell-командами, кодом, Git-репозиториями, внешними инструментами, MCP-серверами, пользовательской памятью и долгими задачами.

Главная идея проекта:

> Svarog — это Git-native runtime для self-improving ИИ-агентов, которые приобретают и улучшают навыки через скиллы, работают в контролируемой среде и оставляют проверяемый след всех действий.

Svarog должен позволять разработчику или организации построить собственного агента уровня OpenClaw/Hermes/OpenCode/Claude Code, но с открытой архитектурой, self-hosted установкой, контролем безопасности и возможностью адаптации под корпоративные или персональные сценарии.

## 2. Итоговое видение

Svarog должен стать универсальным open-source фундаментом для построения персональных, корпоративных, DevOps, coding, research и assistant-агентов.

Пользователь должен иметь возможность:

* развернуть Svarog локально, на сервере или в корпоративном контуре;
* подключить любую LLM через OpenAI-compatible API, LiteLLM, vLLM, Ollama, OpenRouter или другой провайдер;
* создать Git-репозиторий агента, содержащий память, скиллы, проектный контекст и артефакты;
* общаться с агентом через CLI, Telegram, Web UI или API;
* давать агенту задачи, требующие работы с файлами, кодом, документацией, shell-командами и внешними инструментами;
* контролировать опасные действия через approval-механизм;
* видеть trace всех решений, вызовов инструментов, изменений файлов и коммитов;
* позволять агенту предлагать новые скиллы или улучшения существующих скиллов через безопасный review-процесс;
* запускать долгие задачи, которые переживают переполнение контекста и продолжаются через сохраненное состояние в Git.

Svarog должен быть не готовым “одним агентом”, а **платформой для сборки агентов**.

Формула проекта:

```text
Svarog =
  Harness Runtime
+ Skill System
+ Git-native Memory
+ Sandboxed Execution
+ MCP/Tool Ecosystem
+ Refuel Loops
+ Backpressure Checks
+ Approval Policies
+ Multi-interface Gateway
```

## 3. Основные принципы

### 3.1. Harness, not graph

Svarog не должен требовать от пользователя проектирования графов поведения.

Основная модель исполнения:

```text
observe → build context → reason → select skill/tool → act → observe result → verify → continue or finish
```

Графы, workflow и state machines могут существовать как дополнительные механизмы, но не должны быть фундаментом платформы.

### 3.2. Skills over tools

Svarog должен отличать низкоуровневые инструменты от высокоуровневых скиллов.

Пример:

```text
Tool: выполнить bash-команду
Skill: развернуть LLM-модель на GPU-сервере с healthcheck и rollback
```

Tools — это примитивы исполнения.

Skills — это переиспользуемые пакеты знаний, инструкций, скриптов, шаблонов и процедур.

### 3.3. Git-native memory

Долгосрочная память агента должна храниться в человекочитаемом виде в Git-репозитории.

Git должен использоваться для:

* скиллов;
* проектной памяти;
* пользовательских предпочтений;
* решений и архитектурных заметок;
* task summaries;
* артефактов;
* конфигураций;
* истории изменений;
* review и rollback.

При этом runtime-состояние, очереди, locks, traces и быстрые operational-данные должны храниться в специализированных хранилищах: SQLite/Postgres/Redis/Qdrant.

### 3.4. Progressive disclosure

В основной контекст модели не должны загружаться все скиллы полностью.

В контекст передаются только краткие карточки скиллов:

```yaml
name: model-deployment
description: Deploy and validate LLM model services on GPU servers.
risk: high
```

Полный `SKILL.md` загружается только тогда, когда агент решил использовать конкретный скилл.

### 3.5. Sandbox-first

Любое выполнение shell-команд, скриптов, операций с файлами и внешней сетью должно выполняться в контролируемой среде.

Svarog должен поддерживать:

* Docker sandbox;
* локальный workspace;
* ограничение прав пользователя;
* ограничение сети;
* ограничение CPU/RAM;
* таймауты;
* allowlist/denylist путей;
* запрет доступа к секретам без явного разрешения;
* аудит всех выполненных команд.

### 3.6. Human approval by risk

Svarog по умолчанию работает автономно: подтверждения человека требуются только в крайних, действительно важных случаях. Безопасность автономной работы обеспечивается sandbox-enforcement, обратимостью действий (ветки, коммиты, rollback) и полным trace — а не частыми вопросами пользователю.

Действия должны классифицироваться по уровню риска:

```text
low       — чтение файлов, поиск, анализ, создание черновиков;
medium    — изменение локальных файлов, запуск тестов, создание новых артефактов;
high      — git push, удаление файлов, сетевые команды, изменение сервисов;
critical  — продовый деплой, работа с секретами, платежами, пользовательскими данными.
```

Требование approval зависит от режима автономии (autonomy profile):

```text
supervised — approval для high и critical;
auto       — approval для critical, notify для high;
yolo       — approval только для неотключаемого critical-набора, notify для high.
```

`yolo` — режим по умолчанию: основной сценарий использования — автономная работа агента. Режим фиксируется при старте run и не может быть изменен во время исполнения (см. раздел 12). Пост-MVP дополнительно вводится режим `plan`: агент строит план действий без исполнения (концепция из HKUDS OpenHarness).

`notify` означает: действие выполняется сразу, без остановки run'а; пользователь получает асинхронное уведомление, действие выделяется в trace.

Даже в `yolo` approval обязателен и **не отключается конфигурацией** для:

* продового деплоя и изменения работающих сервисов;
* выдачи секретов tool/skill, любых операций с платежами;
* необратимого удаления данных вне workspace;
* force-push и переписывания истории общих веток;
* merge/push в защищенные ветки (main/production);
* ослабления политик безопасности (включение сети sandbox, отключение secret scan).

Critical-набор определяется только по типизированным операциям (deploy-tools, SecretStore, gitflow-компонент), а не по bash-эвристикам: bash физически не может выполнить critical-действие из sandbox без сети и секретов. Эвристики могут эскалировать действие до notify, но не могут разрешить critical (см. ADR-0010).

Классификация по риску имеет два уровня достоверности:

* для типизированных tools (`write_file`, `git.push`, `file.delete`) риск определяется детерминированно по имени tool и аргументам;
* для произвольных shell-команд (`bash`) статическая классификация принципиально ненадежна (команду можно замаскировать) и используется только как UX-эвристика.

Реальные гарантии безопасности для shell-исполнения обеспечиваются enforcement-механизмами sandbox — отключенной сетью, ограниченной файловой системой, отсутствием секретов в окружении — а не распознаванием «опасных команд». Подробнее см. раздел 12 и ADR-0002.

### 3.7. Backpressure

Агент не должен сам безусловно решать, что задача выполнена успешно.

Svarog должен поддерживать автоматические проверки:

* unit tests;
* linters;
* type checks;
* shellcheck;
* markdownlint;
* secret scanning;
* policy checks;
* healthchecks;
* skill-specific checks;
* verifier step.

Если проверка не проходит, агент должен либо исправить результат, либо откатить изменения, либо запросить помощь пользователя.

### 3.8. Refuel loop

Долгие задачи не должны выполняться в одном бесконечно растущем контексте.

Svarog должен поддерживать механизм refuel:

```text
1. Сохранить состояние задачи в task_state.md.
2. Сформировать summary.
3. Зафиксировать изменения в Git.
4. Завершить текущую сессию.
5. Запустить новую сессию из сохраненного состояния.
6. Продолжить задачу.
```

Это позволяет агенту работать над задачами, занимающими часы или дни, без деградации качества из-за переполнения контекста.

### 3.9. Self-improvement через review

Агент должен иметь возможность предлагать новые скиллы и улучшения существующих скиллов, но не должен silently-mutating менять production-навыки без контроля.

Правильный flow:

```text
agent solves task
→ detects reusable pattern
→ proposes new skill or skill update
→ creates branch
→ writes/updates SKILL.md and scripts
→ runs checks
→ shows diff
→ waits for approval
→ merge after review
```

### 3.10. Model-agnostic

Svarog не должен зависеть от конкретного LLM-провайдера.

Обязательная поддержка:

* OpenAI-compatible API;
* LiteLLM;
* vLLM;
* Ollama;
* OpenRouter;
* локальные модели;
* корпоративные LLM endpoints.

Модель должна быть заменяемым компонентом.

## 4. Основные сценарии использования

Svarog должен поддерживать следующие классы сценариев:

### 4.1. Персональный агент

Агент помогает пользователю вести проекты, помнить решения, работать с задачами, файлами, заметками, отчетами, документами и личными автоматизациями.

Примеры:

* “подготовь отчет за последние 2 недели”;
* “обнови описание проекта в памяти”;
* “создай скилл для повторяющейся задачи”;
* “посмотри мои заметки и собери план”.

### 4.2. Coding agent

Агент работает с репозиториями, кодом, тестами и документацией.

Примеры:

* “найди баг и предложи исправление”;
* “добавь FastAPI endpoint и тесты”;
* “обнови README”;
* “сделай refactor и проверь линтерами”.

### 4.3. DevOps/Infrastructure agent

Агент помогает управлять серверами, контейнерами, моделями, GPU и деплоем.

Примеры:

* “проверь, какие модели запущены на сервере”;
* “подготовь план деплоя новой LLM”;
* “посмотри логи и найди причину падения”;
* “сделай rollback по инструкции”.

### 4.4. Research/RAG agent

Агент работает с документацией, базами знаний, Confluence, PDF, markdown-архивами и внутренними документами.

Примеры:

* “найди в документации, как устроен этот сервис”;
* “собери краткое резюме по проекту”;
* “обнови проектную память на основе новых документов”.

### 4.5. Corporate assistant

Агент используется внутри компании в закрытом контуре.

Требования:

* self-hosted режим;
* работа без внешнего интернета;
* локальные модели;
* локальные embeddings;
* локальный vector store;
* контроль секретов;
* аудит;
* разграничение пользователей;
* запрет опасных действий без approval.

## 5. Архитектура системы

Общая архитектура:

```text
Interfaces
  CLI / Telegram / Web UI / REST API / WebSocket
        |
        v
Svarog Gateway
  auth / sessions / users / streaming / approvals
        |
        v
Svarog Runtime
  agent loop
  context builder
  skill loader
  tool router
  policy engine
  memory manager
  refuel manager
  verifier
        |
        v
Execution Layer
  sandbox
  workspace
  file tools
  shell tools
  git tools
  MCP tools
        |
        v
Storage Layer
  Git repositories
  SQLite/Postgres
  Redis
  Qdrant/vector DB
  object storage/filesystem
        |
        v
Observability
  traces
  logs
  tool calls
  diffs
  approvals
  metrics
```

## 6. Ключевые компоненты

### 6.1. Svarog Gateway

Gateway отвечает за внешние интерфейсы и пользовательские сессии.

Функции:

* прием сообщений от пользователя;
* маршрутизация запросов в runtime;
* streaming ответов;
* управление сессиями;
* хранение истории диалога;
* отображение статусов задач;
* обработка approval-запросов;
* интеграция с CLI, Telegram, Web UI, REST API и WebSocket.

Gateway не должен содержать бизнес-логику агента. Он является транспортным и пользовательским слоем.

### 6.2. Agent Runtime

Runtime — ядро Svarog.

Функции:

* управление жизненным циклом agent run;
* построение контекста;
* вызов LLM;
* выбор tools и skills;
* обработка результатов;
* управление итерациями;
* остановка при лимитах;
* запуск verifier;
* обработка ошибок;
* сохранение trace;
* переход в refuel loop при необходимости.

Runtime должен быть детерминированным настолько, насколько возможно. LLM принимает решения только внутри ограниченной среды, заданной runtime, policies и доступными инструментами.

### 6.3. Context Builder

Context Builder формирует входной контекст для модели.

Он должен собирать контекст слоями:

```text
system instructions
developer/project instructions
user profile
current task
recent conversation
project memory
relevant documents
available skill cards
available tools
previous tool results
constraints and policies
```

Context Builder должен учитывать context budget и не допускать бесконтрольного раздувания промпта.

Обязательные функции:

* сжатие истории;
* retrieval релевантной памяти;
* включение только нужных документов;
* включение кратких карточек скиллов;
* исключение устаревших/нерелевантных данных;
* сохранение ссылок на источники контекста.

### 6.4. Skill Loader

Skill Loader отвечает за обнаружение, индексацию и загрузку скиллов.

Функции:

* сканирование директорий `skills/`;
* чтение metadata из `SKILL.md`;
* генерация skill cards;
* передача skill cards в контекст;
* загрузка полного skill только on-demand;
* логирование использования скиллов;
* проверка прав доступа;
* запуск skill-specific checks;
* поддержка skill versioning.

### 6.5. Tool Router

Tool Router отвечает за безопасный вызов низкоуровневых инструментов.

Базовые tools:

```text
read_file
write_file
edit_file
list_dir
search_files
bash
git
read_skill
create_skill_proposal
run_checks
ask_user
request_approval
```

Дополнительно:

* MCP tools;
* HTTP tools;
* browser/search tools;
* RAG tools;
* code execution tools;
* custom enterprise tools.

Каждый tool должен иметь:

```text
name
description
input_schema
output_schema
risk_level
timeout
permissions
sandbox_requirement
approval_requirement
audit_policy
```

### 6.6. Policy Engine

Policy Engine принимает решения о том, разрешено ли агенту выполнить действие.

Он должен учитывать:

* пользователя;
* workspace;
* skill;
* tool;
* аргументы tool call;
* уровень риска;
* текущий режим безопасности;
* наличие approval;
* путь к файлам;
* сетевые ограничения;
* секреты;
* окружение выполнения.

Policy Engine должен уметь возвращать решения:

```text
allow             — действие разрешено;
notify            — действие разрешено и выполняется сразу, пользователю уходит асинхронное уведомление;
deny              — действие запрещено, агент получает причину отказа;
require_approval  — исполнение приостанавливается до решения человека.
```

Итоговое решение зависит от режима автономии (см. 3.6): один и тот же risk level в `supervised` дает require_approval, а в `yolo` — notify. Неотключаемый critical-набор дает require_approval в любом режиме.

К решению `allow` могут прикрепляться execution constraints (например, `sandbox: required`, `network: denied`, `timeout: 30`) — это не отдельные решения, а условия исполнения.

Расширенные типы решений (`require_dry_run`, `require_human_review`) могут добавляться позже как plugin-правила, но не входят в базовую модель: их семантика должна быть определена до внедрения.

Для bash-команд Policy Engine работает как эвристика поверх sandbox-гарантий (см. 3.6). Для MCP tools без явно назначенного risk level действует правило по умолчанию: `risk: high`, require_approval, пока администратор не задал иное.

### 6.7. Memory Manager

Memory Manager управляет памятью агента.

Типы памяти:

```text
dialogue memory
user memory
project memory
task memory
skill memory
semantic memory
operational memory
```

Git используется для долговременной человекочитаемой памяти.

Postgres/SQLite используется для runtime-состояния.

Qdrant/vector DB используется для semantic retrieval.

Redis используется для очередей, locks и временного состояния.

Memory Manager должен поддерживать:

* чтение памяти;
* обновление памяти;
* summary;
* retrieval;
* дедупликацию;
* лимит размера memory-entrypoint, загружаемого в контекст (байты/строки), с политикой усечения;
* версионирование;
* ссылки на источники;
* обновление Git-файлов памяти через controlled flow.

Конкурентный доступ к Git-памяти:

* агентные run'ы не коммитят в memory-репозиторий напрямую — они формируют структурированные memory change requests;
* запись выполняет единственный writer-процесс (memory commit queue), применяющий изменения последовательно;
* конфликтующие изменения разрешаются по принципу last-writer-wins, полная история сохраняется в Git;
* это позволяет нескольким интерфейсам (CLI, Telegram, Web) и параллельным run'ам работать без merge-конфликтов в markdown-файлах (см. ADR-0004).

### 6.8. Git Workspace Manager

Svarog работает с Git в трех разных режимах (flow), которые нельзя смешивать — у них разные правила коммитов, веток и approval (см. ADR-0003).

#### Flow A: память агента (`agent-home/memory`)

* прямые коммиты без веток и review;
* коммиты выполняет единственный writer-процесс (см. 6.7);
* push в remote (если настроен) — фоновый, без approval;
* история Git служит журналом изменений и механизмом rollback.

#### Flow B: скиллы (`agent-home/skills`)

* изменения только через skill proposal: ветка + diff + checks;
* merge после явного approval (см. 3.9 и раздел 18);
* прямые коммиты агента в active skills запрещены policy.

#### Flow C: рабочие репозитории пользователя (код, документация)

* git pull перед началом задачи;
* создание task branch;
* commit после завершения логического шага;
* push в task branch — по режиму автономии (в `yolo` — автоматически, с notify);
* merge/push в защищенные ветки (main/production) — только после approval, в любом режиме;
* diff generation, обработка merge conflicts, rollback.

Правило для Flow C:

```text
pull before work
work in branch
commit meaningful changes
push to task branch per autonomy profile
merge to protected branches only with approval
```

Общие функции менеджера: отслеживание изменений, хранение task summaries, синхронизация состояния между интерфейсами.

Общее правило для всех трех flow: перед каждым commit выполняется обязательный secret scan; commit с обнаруженным секретом блокируется (см. раздел 12, «Секреты и Git-репозитории»).

Git-операции, требующие credentials (push, pull приватных репозиториев), выполняет привилегированный host-компонент runtime, а не код внутри sandbox: sandbox не содержит git-credentials и по умолчанию не имеет сети (см. раздел 12).

### 6.9. Sandbox Manager

Sandbox Manager отвечает за изоляцию исполнения.

Минимальные требования:

* Docker-based sandbox;
* отдельный workspace mount;
* non-root user;
* ограничение CPU/RAM;
* timeout;
* network off by default;
* configurable network allowlist;
* ограниченный доступ к filesystem;
* запрет доступа к host secrets;
* audit всех команд.

Расширенные режимы:

* Kubernetes Job;
* remote runner;
* gVisor;
* Firecracker;
* air-gapped mode;
* local trusted mode.

### 6.10. Refuel Manager

Refuel Manager отвечает за продолжение долгих задач между сессиями.

Функции:

* определение необходимости refuel;
* создание `task_state.md`;
* summary текущего состояния;
* сохранение открытых вопросов;
* сохранение плана дальнейших действий;
* commit состояния;
* запуск новой агентной сессии;
* восстановление контекста из task_state и Git.

Разграничение с Context Builder (два механизма против переполнения контекста):

* **compaction** (сжатие истории, 6.3) — механизм внутри одного run: история диалога и tool-результаты суммируются, run продолжается;
* **refuel** — механизм между run'ами: состояние сериализуется в `task_state.md` + Git, сессия завершается, новая сессия пересобирает контекст с нуля;
* compaction применяется первым; refuel срабатывает, когда compaction уже не спасает (порог итераций/токенов из конфигурации) или когда run прерван (approval-ожидание, лимит стоимости, падение процесса).

Refuel — частный случай общего механизма resumable runs (см. раздел 11 и ADR-0005).

### 6.11. Verifier

Verifier проверяет качество результата.

Возможные проверки:

* запуск тестов;
* запуск линтеров;
* проверка типов;
* проверка форматирования;
* проверка наличия секретов;
* проверка policy violations;
* проверка соответствия user request;
* LLM-as-judge;
* human review.

Verifier должен быть отделен от основного исполнителя, чтобы снизить риск самоподтверждения ошибки.

Отделение означает:

* verifier запускается отдельным LLM-вызовом с чистым контекстом — без истории рассуждений исполнителя;
* verifier получает только: исходную задачу, итоговый diff/артефакты, результаты автоматических проверок;
* опционально verifier может использовать другую модель;
* детерминированные проверки (тесты, линтеры, secret scanning) всегда имеют приоритет над LLM-as-judge: LLM-judge не может «перекрыть» упавший тест.

### 6.12. Observability

Svarog должен сохранять полный trace каждого запуска.

Trace должен включать:

```text
run_id
user_id
workspace_id
model
input
context sources
loaded skills
tool calls
tool outputs
file changes
git diffs
approvals
errors
checks
final answer
cost
duration
token usage
```

Trace должен быть доступен через CLI/Web/API.

Trace-данные растут неограниченно, поэтому конфигурация должна поддерживать retention policy: например, сырые tool outputs удаляются через N дней, а метаданные, решения и approvals сохраняются.

## 7. Формат скиллов

Базовая структура скилла:

```text
skills/
  skill-name/
    SKILL.md
    scripts/
    templates/
    examples/
    tests/
    data/
```

Единственный обязательный файл — `SKILL.md`.

Пример структуры:

```text
skills/
  model-deployment/
    SKILL.md
    scripts/
      check_gpus.sh
      plan_deploy.py
      deploy.py
      healthcheck.sh
      rollback.sh
    templates/
      deployment.yaml
    tests/
      test_skill.py
```

Пример `SKILL.md`:

```markdown
---
name: model-deployment
description: Deploy, validate and rollback LLM model services on GPU servers.
version: 0.1.0
risk: high
allowed_tools:
  - read_file
  - write_file
  - bash
  - git
requires_approval:
  - bash.network
  - git.push
  - service.restart
---

# When to use

Use this skill when the user asks to deploy, update, stop, validate or rollback an LLM model.

# Workflow

1. Pull latest repo state.
2. Inspect deployment config.
3. Check available GPUs.
4. Create deployment plan.
5. Ask for approval before changing running services.
6. Execute deployment.
7. Run healthcheck.
8. Save report.
9. Commit changes.

# Failure handling

If healthcheck fails, rollback and write an incident report.
```

Skill metadata должна быть машинно-читаемой.

Формат `SKILL.md` должен быть совместим с открытым стандартом [agentskills.io](https://agentskills.io) (его используют hermes-agent и HKUDS OpenHarness) — это дает доступ к экосистеме готовых скиллов. Поля Svarog, отсутствующие в стандарте (`risk`, `requires_approval`, `checks`), — расширение frontmatter, не ломающее совместимость.

Обязательные поля:

```text
name
description
version
risk
```

Поле `version` — semver, используется для зависимостей между скиллами и отображения в skill cards; полная история изменений хранится в Git и не дублируется в metadata.

Опциональные поля:

```text
allowed_tools
required_tools
requires_approval
dependencies
environment
inputs
outputs
checks
owner
tags
```

## 8. Репозиторий агента

Svarog должен предполагать наличие agent-home репозитория.

Пример:

```text
agent-home/
  AGENTS.md
  README.md

  skills/
    report-writer/
    model-deployment/
    confluence-rag/
    llm-server-admin/
    skill-curator/

  memory/
    user/
      profile.md
      preferences.md
      long_term_memory.md

    projects/
      project-a.md
      project-b.md

    decisions/
      architecture-decisions.md

  workspaces/
    tasks/

  artifacts/
    reports/
    diagrams/
    generated-files/

  policies/
    security.yaml
    approvals.yaml
    tools.yaml

  evals/
    checks/
```

`AGENTS.md` должен содержать основные инструкции агента для данного репозитория.

Пример содержания:

```markdown
# Agent instructions

- Always run git pull before starting work.
- Work in a task branch.
- Do not push without approval.
- Use skills when applicable.
- Save reusable procedures as skill proposals.
- Do not access secrets unless explicitly approved.
- Run checks before reporting completion.
```

## 9. Работа с MCP

Svarog должен поддерживать MCP как стандарт подключения внешних инструментов.

MCP tools должны регистрироваться в Tool Registry и проходить через Policy Engine.

Требования:

* подключение MCP servers;
* discovery доступных tools;
* описание tools в контексте;
* permission model;
* audit вызовов;
* sandbox/approval при необходимости;
* отключение небезопасных MCP tools;
* поддержка локальных и удаленных MCP servers;
* MCP tool без явно назначенного risk level по умолчанию получает `risk: high` и требует approval, пока администратор не задал иное.

Skills и MCP должны быть разными уровнями:

```text
MCP/tool — действие, которое агент может выполнить.
Skill — инструкция, когда и как использовать tools для достижения цели.
```

## 10. Интерфейсы

### 10.1. CLI

CLI должен позволять:

```text
svarog init
svarog run "task"
svarog chat
svarog skills list
svarog skills add
svarog skills check
svarog skills curate
svarog traces list
svarog approvals list
```

CLI должен быть основным интерфейсом для разработчиков.

Флаги `--yolo` / `--auto` / `--supervised` переопределяют `runtime.autonomy` из конфигурации для конкретного запуска.

Семантика: `run` создает один agent run на задачу; `chat` открывает интерактивную сессию, в которой каждое сообщение пользователя порождает отдельный run, связанный общей session и общим workspace-контекстом. Trace всегда привязан к run; session агрегирует runs.

### 10.2. Telegram

Telegram-интерфейс должен позволять:

* отправлять задачи агенту;
* получать streaming/log updates;
* видеть запросы approval;
* подтверждать или отклонять действия;
* получать артефакты;
* продолжать задачи между сессиями.

### 10.3. Web UI

Web UI должен предоставлять:

* список задач;
* текущие agent runs;
* trace viewer;
* skill browser;
* approval inbox;
* diff viewer;
* artifacts;
* memory browser;
* настройки моделей и tools.

### 10.4. REST/WebSocket API

API должен позволять встраивать Svarog в другие системы.

Функции:

* создать run;
* отправить сообщение;
* получить stream событий;
* выдать approval;
* получить trace;
* получить список skills;
* получить diff;
* получить артефакты.

## 11. Модель исполнения agent run

Agent run — это возобновляемый процесс (state machine), а не линейный вызов:

```text
pending → running → completed | failed | cancelled
running → waiting_approval → running   (после решения человека)
running → suspended → running          (refuel, лимиты, рестарт процесса)
```

Типовой agent run:

```text
1. Receive user task.
2. Load user/session/workspace context.
3. Pull Git workspace if enabled.
4. Build context with relevant memory and skill cards.
5. Ask LLM for next action.
6. If skill needed, load SKILL.md.
7. If tool needed, pass through Policy Engine.
8. Execute tool in sandbox if required.
9. Observe result.
10. Continue loop.
11. Run verifier/checks.
12. Save artifacts.
13. Commit meaningful changes.
14. Ask approval for push or risky actions.
15. Return final answer with summary and links.
```

Любая приостановка — ожидание approval, refuel, исчерпание бюджета, падение процесса — сохраняет checkpoint: состояние run'а, контекст задачи и незавершенные шаги. Run возобновляется из checkpoint без потери прогресса.

Ожидание approval не блокирует процесс: run переходит в `waiting_approval`, sandbox-ресурсы освобождаются или замораживаются, решение может прийти через любой интерфейс спустя часы. Это базовое требование модели исполнения, а не опция «для долгих задач» (см. ADR-0005).

## 12. Безопасность

Svarog должен проектироваться как система, в которой LLM считается недоверенным компонентом.

Основные требования:

* LLM не получает прямой неограниченный доступ к host-системе;
* все tools проходят через Policy Engine;
* все shell-команды выполняются в sandbox по умолчанию;
* секреты не доступны автоматически;
* сетевой доступ отключен по умолчанию;
* действия из неотключаемого critical-набора (см. 3.6) требуют approval в любом режиме автономии;
* остальные рискованные действия обрабатываются по режиму автономии: approval, notify или auto;
* merge/push в защищенные ветки требует approval;
* все действия логируются;
* skills из внешних источников считаются недоверенными до review;
* community skills должны иметь permission manifest;
* prompt injection из файлов и документов должен учитываться как риск;
* инструкции из документов не должны автоматически переопределять system/developer policies.

### Механизм работы с секретами

* секреты хранятся в secret store (шифрованный файл, env, внешний vault — pluggable backend);
* секреты инжектируются на execution-слое: в окружение sandbox, в конфигурацию MCP-коннектора, в git credential helper;
* секреты никогда не передаются через контекст LLM и вырезаются из trace и tool outputs (redaction);
* агент оперирует только именами секретов (named references), не значениями;
* выдача секрета конкретному tool/skill — policy-решение с уровнем риска `critical`;
* git push выполняется host-компонентом с credentials, недоступными sandbox.

### Секреты и Git-репозитории

Svarog — открытый проект, а репозитории, с которыми работает агент (agent-home, рабочие репозитории), могут иметь публичные remotes. Поэтому каждый коммит считается потенциально публичным:

* обязательный pre-commit secret scan во всех трех git flows (память, скиллы, код пользователя): при обнаружении секрета commit блокируется, а не предупреждает;
* файлы секретов (`.env`, key-файлы, secret store) входят в denylist путей для `write_file` и для коммитов; `svarog init` создает `.gitignore` с этими паттернами;
* push дополнительно проверяется тем же сканером (вторая линия — на случай коммитов, сделанных в обход агента);
* secret scan нельзя отключить policy для репозиториев с публичным remote;
* если секрет все же попал в историю — он считается скомпрометированным: процедура — ротация секрета, затем очистка истории; простое удаление файла следующим коммитом не решает проблему.

### Защита от prompt injection

* содержимое файлов, документов, web-страниц и tool-результатов помечается в контексте как untrusted data;
* инструкции из untrusted data не могут инициировать approval, изменять policies или расширять права;
* approval-запрос всегда показывает человеку фактическое действие (команду, diff, аргументы), а не пересказ агента;
* режим автономии и policy-конфигурация фиксируются при старте run и не перечитываются из окружения/файлов во время исполнения — эскалация режима (например, выставление yolo-переменной изнутри скилла) невозможна по построению (прием из hermes-agent, см. docs/reference-analysis.md).

## 13. Конфигурация

Svarog должен иметь конфигурацию на уровне проекта и пользователя.

Пример `svarog.yaml`:

```yaml
models:
  default: local-qwen
  auxiliary: local-qwen   # дешевая модель для служебных задач: curator, компакция, verifier-judge
  providers:
    local-qwen:
      type: openai-compatible
      base_url: http://localhost:8000/v1
      model: qwen3-coder
      # api_key_ref: PROVIDER_API_KEY  # именованная ссылка на секрет, не значение (ADR-0006)
      # input_usd_per_mtok: 3.0        # цены за 1M токенов для учета стоимости run
      # output_usd_per_mtok: 15.0      # 0 (по умолчанию) — локальная модель
      # timeout_sec: 120
      # max_retries: 2

runtime:
  autonomy: yolo            # supervised | auto | yolo (по умолчанию)
  max_iterations: 50
  max_context_tokens: 120000
  refuel_after_iterations: 35
  max_tokens_per_run: 2000000
  max_cost_usd_per_run: 5.0

sandbox:
  type: docker
  network: disabled
  memory_limit: 8g
  cpu_limit: 4
  timeout_sec: 120

git:
  auto_pull: true
  auto_commit: true
  require_approval_for_push: true
  secret_scan_before_commit: true   # нельзя отключить для репозиториев с публичным remote

skills:
  paths:
    - ./skills
    - ~/.svarog/skills
  auto_load_full_content: false

policies:
  # неотключаемый critical-набор (см. 3.6) требует approval всегда
  # и в конфигурации не перечисляется
  protected_branches:
    - main
    - production
  profiles:
    yolo:
      require_approval: []      # сверх critical-набора — ничего
      notify:
        - git.push
        - file.delete
        - bash.network
    supervised:
      require_approval:
        - git.push
        - file.delete
        - bash.network
        - service.restart
```

## 14. Хранилища

Svarog должен использовать несколько типов хранилищ. Они делятся на обязательные и опциональные: базовая установка (`svarog init`) должна работать только на Git + SQLite, без единого внешнего сервиса. Redis и vector DB — pluggable backends для server/corporate режимов; в базовой установке их функции выполняют SQLite и in-process механизмы (см. ADR-0007).

### Git

Для:

* skills;
* memory;
* artifacts;
* project state;
* decisions;
* task summaries.

### SQLite (по умолчанию) / Postgres (server-режимы)

Для:

* users;
* sessions;
* runs;
* tool calls;
* approvals;
* trace metadata;
* operational state.

### Redis (опционально)

Для:

* queues;
* locks;
* temporary state;
* streaming events.

В базовой установке эти функции выполняют SQLite-таблицы и in-process механизмы; Redis подключается для multi-process/server развертываний.

### Vector DB (опционально)

Для:

* semantic search по memory;
* semantic search по skills;
* semantic search по документации;
* retrieval проектного контекста.

Qdrant должен быть рекомендуемым вариантом, но архитектура должна позволять использовать другие vector DB.

## 15. Логирование и аудит

Каждое действие агента должно быть воспроизводимо и проверяемо.

Обязательные сущности аудита:

```text
AgentRun
Message
ContextSnapshot
SkillLoad
ToolCall
ToolResult
ApprovalRequest
ApprovalDecision
FileChange
GitCommit
CheckResult
Artifact
ErrorEvent
```

Trace должен позволять ответить на вопросы:

* почему агент выбрал этот skill;
* какие файлы он прочитал;
* какие команды выполнил;
* какие изменения сделал;
* какие проверки прошли или упали;
* кто подтвердил опасное действие;
* какой итоговый результат был получен.

## 16. Роли пользователей

Svarog должен поддерживать базовые роли:

```text
owner
admin
developer
operator
viewer
agent
```

Роли определяют:

* доступ к workspace;
* доступ к skills;
* право approving risky actions;
* право редактировать policies;
* право подключать tools;
* право просматривать traces;
* право управлять моделями.

## 17. Режимы работы

Svarog должен поддерживать несколько режимов безопасности:

### Local trusted

Для локальных экспериментов.

* меньше ограничений;
* sandbox может быть отключен;
* пользователь осознанно принимает риск.

### Local sandboxed

Для обычной разработки.

* Docker sandbox;
* ограниченный filesystem;
* approval для risky actions.

### Server personal

Для персонального агента на VPS/home server.

* Telegram/Web;
* отдельный service user;
* sandbox;
* Git memory;
* approval.

### Corporate self-hosted

Для закрытого контура.

* локальные модели;
* локальные хранилища;
* без внешних API;
* audit;
* RBAC;
* strict policies;
* offline skills;
* enterprise secrets handling.

## 18. Skill governance

Скиллы должны иметь жизненный цикл:

```text
draft
active
deprecated
archived
blocked
```

Svarog должен поддерживать:

* создание skill proposal;
* проверку skill;
* review diff;
* merge после approval;
* архивирование неиспользуемых skills;
* анализ дубликатов;
* консолидацию похожих skills;
* версионирование;
* rollback skill changes;
* журнал использования.

Агент может предлагать:

* создать новый skill;
* обновить существующий skill;
* добавить пример;
* добавить тест;
* архивировать skill;
* объединить похожие skills.

Но применение изменений должно проходить через governance flow.

### 18.1. Skill Curator

Skill Curator — компонент, поддерживающий здоровье библиотеки скиллов по мере ее роста. Без кураторства библиотека деградирует: накапливаются дубликаты, устаревшие и сломанные скиллы, а skill cards перестают помогать модели выбирать нужный навык.

Curator работает в два слоя:

#### Слой 1: механический pruning (без LLM, дешевый)

Детерминированные проверки по метаданным и журналу использования:

* статистика использования из trace: скилл не использовался `stale_after_days` → `stale`, дольше `archive_after_days` → `archived`;
* скиллы с высокой долей неудачных применений (упавшие checks после использования) — кандидаты на fix/deprecate;
* невалидная metadata, битые ссылки на scripts, упавшие skill-specific tests — кандидаты в blocked;
* скиллы, ссылающиеся на несуществующие tools — кандидаты на fix.

Обратимые lifecycle-переходы (`active → stale → archived` и обратная реактивация) слой 1 **применяет автоматически**, без proposals: они затрагивают только agent-created скиллы, ничего не удаляют и откатываются одним действием. Остальные находки — кандидаты для слоя 2.

#### Слой 2: семантическая консолидация (LLM)

Работает по кандидатам слоя 1 и по всей библиотеке:

* поиск дубликатов и пересечений (embedding similarity + LLM-анализ пар);
* предложения по объединению похожих скиллов в один;
* выделение общих процедур из нескольких скиллов в отдельный скилл;
* улучшение `description` в skill cards, чтобы модель точнее выбирала скилл;
* предложения по архивированию с обоснованием.

#### Правила (инварианты, проверенные curator'ом hermes-agent)

* curator работает **только с agent-created скиллами**: официальные и написанные человеком скиллы вне зоны его действия (provenance фиксируется при создании скилла);
* curator **никогда не удаляет** — только архивирует; архив обратим;
* флаг `pinned` выводит скилл из-под любых автоматических переходов;
* скилл, на который ссылается любая scheduled-задача (включая приостановленные), считается используемым и не архивируется;
* новый скилл получает якорь `created_at`, чтобы не попасть под архивацию до первого использования;
* содержательные изменения (consolidation, merge, правки текста скилла) — **только через skill proposals** (Flow B из 6.8); автоматически применяются лишь обратимые lifecycle-переходы слоя 1;
* слой 2 работает на auxiliary-модели (см. §13) и выключен по умолчанию (opt-in);
* запуск: в моменты простоя агента по интервалу, после добавления N новых скиллов, вручную через `svarog skills curate`;
* каждый прогон curator оставляет отчет (что найдено, что изменено, что предложено) в trace и в `artifacts/`;
* consolidation не должна терять информацию: объединяемые скиллы архивируются, а не удаляются.

## 19. Backpressure и quality gates

Svarog должен поддерживать quality gates на нескольких уровнях:

### Уровень задачи

* task-specific checks;
* тесты;
* verifier;
* human review.

### Уровень skill

* наличие metadata;
* валидность `SKILL.md`;
* разрешенные tools;
* тесты skill;
* отсутствие секретов;
* корректные scripts.

### Уровень workspace

* git status clean/dirty;
* branch policy;
* protected files;
* policy violations.

### Уровень безопасности

* secret scanning;
* dangerous command detection;
* network policy;
* filesystem policy;
* approval requirements.

## 20. Работа с долгими задачами

Svarog должен позволять агенту выполнять задачи, которые не помещаются в одну LLM-сессию.

Для этого используются:

* task state;
* summaries;
* commits;
* refuel loop;
* resumable runs;
* checkpoints;
* branch-based attempts.

Файл `task_state.md` должен содержать:

```text
current goal
completed steps
remaining steps
important findings
files changed
commands run
open questions
next recommended action
risks
```

## 21. Взаимодействие с пользователем

Агент должен быть честным и наблюдаемым.

Он должен сообщать:

* что он собирается делать;
* когда требуется approval;
* какие проверки прошли;
* какие проверки упали;
* какие изменения внесены;
* какие ограничения были;
* что не удалось сделать.

Агент не должен:

* скрывать ошибки;
* утверждать, что выполнил действие, если оно не выполнено;
* выполнять действия из неотключаемого critical-набора (3.6) без approval;
* изменять production skills без review;
* игнорировать failing checks;
* самовольно обходить policies.

## 22. Расширяемость

Svarog должен иметь plugin/API-модель.

Расширяемые части:

* model providers;
* tools;
* MCP connectors;
* sandbox backends;
* memory backends;
* vector DB;
* interfaces;
* policy rules;
* verifiers;
* skill registries.

Каждая интеграция должна быть заменяемой.

## 23. Пример официальных скиллов

Svarog должен поставляться с базовым набором official skills:

```text
git-workflow
python-project
fastapi-service
docker-compose
report-writer
skill-authoring
skill-review
skill-curator
markdown-docs
shell-debugging
model-server-admin
```

Каждый official skill должен иметь:

* `SKILL.md`;
* примеры;
* checks;
* tests, если применимо;
* описание risk level;
* список необходимых tools.

## 24. Нефункциональные требования

### Производительность

* низкая задержка для коротких задач;
* streaming событий;
* lazy loading скиллов;
* context budget management;
* возможность работы с большими репозиториями через search/indexing.

### Надежность

* восстановление после падения процесса;
* resumable runs;
* idempotent tool calls, где возможно;
* таймауты;
* retries;
* rollback для рискованных операций.

### Безопасность

* sandbox by default;
* least privilege;
* audit;
* approval;
* secret isolation;
* RBAC;
* policy engine.

### Переносимость

* локальный запуск;
* Docker Compose;
* server deployment;
* air-gapped режим;
* OpenAI-compatible модели;
* отсутствие жесткой привязки к облаку.

### Разработческий опыт

* понятный CLI;
* хорошие docs;
* простая структура skills;
* шаблоны;
* examples;
* trace viewer;
* ясные ошибки.

## 25. Что Svarog не должен делать

Svarog не должен:

* быть еще одним LangGraph-аналогом;
* требовать от пользователя проектировать графы;
* быть привязанным к одному LLM-провайдеру;
* хранить всю память только в vector DB;
* давать агенту полный доступ к host-системе по умолчанию;
* автоматически менять production skills без review;
* скрывать tool calls;
* быть только Telegram-ботом;
* быть только coding assistant;
* быть только RAG-платформой.

Svarog должен быть базовым runtime/harness-слоем, на котором можно строить разные классы агентов.

## 26. Критерии успешной реализации

Проект считается соответствующим видению, если Svarog позволяет:

* создать agent-home Git repo;
* подключить LLM через OpenAI-compatible API;
* обнаружить скиллы из `skills/`;
* передать в контекст только краткие skill cards;
* загрузить полный `SKILL.md` on-demand;
* выполнить задачу с чтением/записью файлов;
* выполнить shell-команду в sandbox;
* применить policies и approval;
* сохранить trace всех действий;
* сделать commit изменений;
* запустить checks;
* предложить новый skill после решения повторяемой задачи;
* продолжить долгую задачу через refuel loop;
* работать через CLI и внешний API;
* быть расширяемым через MCP/tools/plugins.

Каждый критерий из этого списка должен быть оформлен как исполняемый eval-сценарий в `evals/` и запускаться в CI.

## 27. Итоговая формулировка продукта

**Svarog** — это open-source agent runtime для создания безопасных self-hosted ИИ-агентов, которые работают с Git-памятью, скиллами, sandboxed tools, MCP-интеграциями и долгими задачами.

Svarog должен дать разработчикам возможность строить агентов, которые не просто отвечают в чате, а реально работают в файловой системе, репозиториях, документации, серверах и проектах, при этом оставаясь контролируемыми, воспроизводимыми и безопасными.

Короткая формула:

```text
Svarog — Git-native runtime for self-improving AI agents.
```

Расширенная формула:

```text
Svarog — open-source harness platform for building skill-based, self-hosted AI agents with Git memory, sandboxed execution, MCP tools, refuel loops, approval policies and auditable traces.
```

## 28. MVP: первый вертикальный срез

Полный объем этого ТЗ не реализуется одним этапом. Первая работоспособная версия — минимальный вертикальный срез, проверяющий ключевые архитектурные решения.

Входит в MVP:

* CLI: `init`, `run`, `chat`, `skills list`, `traces list`, approval в терминале;
* agent loop с checkpoint/resume (state machine из раздела 11);
* tools: `read_file`, `write_file`, `edit_file`, `list_dir`, `search_files`, `bash`, `git`, `read_skill`, `ask_user`, `request_approval`;
* Docker sandbox: network off, non-root, CPU/RAM limits, timeout;
* skill loader: сканирование, skill cards, on-demand загрузка `SKILL.md`;
* Policy Engine: allow/notify/deny/require_approval + execution constraints + режимы автономии (`yolo` по умолчанию);
* Git flows A и C (память + рабочий репозиторий); Flow B в MVP — вручную через обычный git-review;
* SQLite: runs, messages, tool calls, approvals, checkpoints, trace;
* один провайдер: OpenAI-compatible API;
* refuel по порогу итераций;
* детерминированные checks (тесты, линтеры) как verifier.

Не входит в MVP (следующие итерации): Telegram, Web UI, REST/WebSocket API, Redis, Qdrant/semantic retrieval, RBAC и multi-user, MCP, автоматизация skill governance, Skill Curator, LLM-as-judge verifier, compaction истории (в MVP роль compaction выполняет refuel).

Подробно — ADR-0008 и `docs/first-issues.md`.

---

Архитектурные решения, снимающие ключевые риски этого ТЗ, зафиксированы в `docs/adr/` (ADR-0001…0010). Структура репозитория — `docs/repo-structure.md`. Список первых issues — `docs/first-issues.md`.
