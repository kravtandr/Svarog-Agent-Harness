<p align="center">
  <img src="assets/logo.png" alt="Svarog Agent Harness" width="480">
</p>

<h1 align="center">Svarog Agent Harness</h1>

<p align="center">
  <a href="https://github.com/kravtandr/Svarog-Agent-Harness/actions/workflows/ci.yml"><img src="https://github.com/kravtandr/Svarog-Agent-Harness/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/deps-Git%20%2B%20SQLite-green.svg" alt="Only Git + SQLite">
</p>

**Svarog** — open-source, self-hosted, Git-native runtime для ИИ-агентов: скиллы, sandboxed execution, Git-память с гиперперсонализацией, планировщик, refuel loops, approval policies, мультиарендность, cloud-режим и полный audit trace. Это платформа для сборки агентов, а не готовый агент и не workflow-фреймворк.

> **Pre-alpha** Работает end-to-end; весь набор unit-тестов и eval-сценарии критериев готовности MVP гоняются в CI; 21 ADR фиксируют архитектурные решения с трейд-оффами. Self-hosted на любом OpenAI-совместимом endpoint — из инфраструктуры только Git + SQLite, без внешних сервисов. Публичного контракта API пока нет — детали могут меняться.

## Quick Start

### 1. Клонировать и поставить зависимости

```bash
git clone git@github.com:kravtandr/Svarog-Agent-Harness.git
cd Svarog-Agent-Harness
uv sync
```

Требования: Python 3.12+ (uv поставит сам), [uv](https://docs.astral.sh/uv/).

### 2. Создать agent-home (интерактив)

```bash
uv run svarog init                # спросит путь, модель, base_url и API-ключ
```

`init` спрашивает каталог agent-home (по умолчанию `./agent-home` внутри проекта), имя модели, `base_url` endpoint и API-ключ; создаёт skills, memory (Flow A), policies, `.gitignore` для секретов; если agent-home лежит внутри проекта — добавляет его в `.gitignore` проекта. Введённый ключ **не** записывается в `svarog.yaml` — он сохраняется в SecretStore, а в конфиг попадает только имя (`api_key_ref`). Для локальных серверов (vLLM, llama.cpp) ключ не нужен — оставьте пустым. Runtime-состояние (traces, SQLite) живёт в `./.svarog/` внутри agent-home и в Git не попадает.

Без интерактива всё задаётся флагами:

```bash
uv run svarog init ./agent-home --no-input \
  --model qwen3-coder --base-url http://localhost:8000/v1 --api-key sk-…
```

Чтобы сразу настроить Claude Code или OpenCode как исполнителя
(`executor.external`, ADR-0016) — независимо друг от друга, credentials
можно не вводить и добавить позже:

```bash
uv run svarog init ./agent-home --no-input \
  --executor claude-code --claude-auth subscription   # OAuth-токен потом: svarog secrets set CLAUDE_CODE_OAUTH_TOKEN

uv run svarog init ./agent-home --no-input \
  --executor opencode --opencode-same-as-native        # те же креды, что и у models.local
```

Вместо `init` можно создать `svarog.yaml` вручную (полная схема — §13 [TASK.md](TASK.md)):

```yaml
models:
  default: local-qwen
  providers:
    local-qwen:
      type: openai-compatible
      base_url: http://localhost:8000/v1   # vLLM, llama.cpp, LiteLLM, OpenRouter…
      model: qwen3-coder
      # api_key_ref: PROVIDER_API_KEY      # имя env-переменной с ключом, если нужен
```

### 3. Подключить `svarog` из любой папки одной командой

`svarog.yaml` создаётся **внутри** agent-home, а `svarog` ищёт его в текущей директории. Чтобы `svarog chat`/`run` работали как `claude` — из любого проекта, без `cd agent-home` — выполните `install`: она пропишет в shell rc переменные окружения + алиас и подключит конфиг agent-home как user-level (symlink на `~/.svarog/svarog.yaml`):

```bash
uv run svarog install
```

Команда идемпотентна — повторный вызов обновляет блок (маркеры `# >>> svarog >>>` … `# <<< svarog <<<`), так что её можно запускать после смены путей. Флаги для нестандартных случаев: `--shell zsh` (по умолчанию auto по `$SHELL`), `--repo PATH` (если checkout Svarog не над agent-home), `--no-symlink`, `--force`. Если `~/.svarog/svarog.yaml` уже существует как файл (например, после `svarog login`) — symlink пропускается с предупреждением; перенесите его содержимое в `agent-home/svarog.yaml` и удалите, либо `--no-symlink`.

Относительные `./memory`, `./skills`, `./.svarog` в `agent-home/svarog.yaml` резолвятся от cwd; env-переменные (приоритетнее yaml) прибивают их к agent-home. После `exec bash` (или нового терминала) `cd` в любой проект и `svarog chat` — workspace = текущая папка, control-plane (память/скиллы/БД/конфиг) фиксирован в agent-home «где создан», `assert_workspace_isolated` (ADR-0015 §0.3) не ругается. Секреты и история чата от cwd не зависят (`~/.svarog/secrets.json`, `~/.svarog/chat_history`). Нюанс: `policies/*.yaml` читается из `<workspace>/policies/`, а не из agent-home — кастомные project-level правила при запуске из папки без своего `policies/` не действуют; неотключаемый critical-набор и risk×autonomy (§3.6) от workspace не зависят и продолжают работать.

### 4. Первый запуск

```bash
svarog run "создай hello.py, который печатает время"   # выполнить задачу
svarog chat                                            # интерактивная сессия
```

### Подробнее про API-ключ

Ключ модели **никогда не хранится в `svarog.yaml`** (схема провайдера строгая — `extra="forbid"`, поля под значение ключа нет). В конфиге указывается лишь имя секрета через `api_key_ref`, значение резолвится на execution-слое через SecretStore (ADR-0006). `init` уже спросил ключ и сохранил его в SecretStore — этого достаточно для старта; для локальных серверов (vLLM, llama.cpp) ключ не нужен.

Задать или переопределить значение можно одним из двух способов (первым срабатывает файл, затем env):

```bash
# 1) файл секретов (FileSecretStore, ~/.svarog/secrets.json, права 0600)
svarog secrets set PROVIDER_API_KEY      # спросит значение, не покажет в истории
svarog secrets list                      # только имена, без значений

# 2) переменная окружения (EnvSecretStore) — имя должно совпадать с api_key_ref
export PROVIDER_API_KEY="sk-…"
```

> `.env` **не подхватывается автоматически** (нет `load_dotenv`) — подгрузите вручную: `set -a; source .env; set +a`. Файлы `.env`, `*.key`, `.svarog/secrets*` уже в `.gitignore` (denylist ADR-0006) и отвергаются git-flow до коммита. Если `api_key_ref` задан, а секрет не найден, run падает с `ApiKeyError` и подсказкой.

## Команды

```bash
svarog run "создай hello.py, который печатает время" # выполнить задачу
svarog chat                                          # интерактивная сессия (диалог из нескольких runs)
                                                      # inline (ADR-0018): welcome + / и @ меню при наборе,
                                                      # tool-карточки, markdown в scrollback; Ctrl+C — прервать;
                                                      # --plain — построчный REPL; запуск из любой папки:
                                                      # пересечение с control-plane требует подтверждения
svarog sessions list                                 # чат-сессии; rename — переименовать
svarog traces list                                   # последние runs
svarog traces show <run-id>                          # полный trace run'а
svarog resume <run-id>                               # продолжить приостановленный run
svarog approvals list                                # ожидающие подтверждения и вопросы
svarog approvals approve <id>                        # или deny <id> --reason "…"
svarog approvals answer <id> "текст"                 # ответить на вопрос ask_user (§6.5)
svarog skills list                                   # доступные скиллы и карточки
svarog skills proposals list                         # skill proposals на review (Flow B)
svarog skills proposals approve <id>                 # влить proposal (или reject <id>)
svarog skills curate [--semantic]                    # Curator: lifecycle (+ LLM-консолидация)
svarog skills pin <name>                             # закрепить скилл (вне авто-переходов)
svarog memory show                                   # память, как она попадёт в контекст
svarog memory flush / curate                         # слить очередь Flow A / lint wiki
svarog memory proposals list                         # Dream-proposals на review (ADR-0020)
svarog memory proposals approve <id>                 # влить proposal (или reject <id>)
svarog cron list                                     # джобы планировщика (ADR-0019)
svarog cron add --task "…" --every 3600              # или --at HH:MM; enable|disable|remove
svarog scheduler                                     # демон расписания (serve джобы НЕ исполняет)
svarog push <branch>                                 # push task-ветки (Flow C, с policy)
svarog serve                                         # REST/WebSocket gateway (extra `server`, §10.4)
svarog telegram                                      # Telegram-бот (§10.2)
svarog mcp list                                      # инструменты MCP-серверов (extra `mcp`, §9)
svarog doctor                                        # диагностика окружения (--clean-orphans)
svarog tenant create alice --role standard           # завести тенанта: свой agent-home + bearer-токен
svarog tenant list                                   # тенанты; token [--rotate] / add-principal — доступ
svarog login <url>                                   # подключиться к удалённому серверу (ADR-0017)
svarog remote run "…" --workspace ws                 # run на сервере — см. «Cloud-режим»
```

## Ключевые особенности

Большинство agent-платформ — либо конструкторы графов и воркфлоу, либо облачные ассистенты с непрозрачной памятью и полным доступом к системе. Svarog — loop-агент (`observe → reason → select skill/tool → act → verify`, как Hermes и OpenClaw, а не граф вроде LangGraph/n8n), но с жёстким backbone:

* **Git-native память вместо чёрного ящика.** Память, решения, задачи и артефакты — человекочитаемые markdown-файлы в Git: их ревьюят, версионируют и откатывают обычным `git log`; single-writer очередь исключает merge-конфликты при параллельных run'ах и интерфейсах. FTS5-поиск по содержимому — без внешней Vector DB.
* **Гиперперсонализация из коробки (ADR-0021).** Типизированный `user/profile.md` становится персона-директивой в системном промпте; автозахват фактов после сессий и Dream (семантическая консолидация) включены по умолчанию — правят память под governance, а не молча.
* **Безопасность через enforcement, а не «распознавание опасных команд».** LLM — недоверенный компонент. Гарантии дают инварианты: sandbox без сети и non-root, секреты только именованными ссылками (redaction в контексте и trace), режим автономии и policy замораживаются при старте run — инъекция из файла не повышает права.
* **Полноценная мультиарендность.** Tenant = свой agent-home (память, скиллы, секреты, БД, workspace); роль **standard** принудительно заперта в docker-sandbox (fail-closed без docker), **superuser** имеет доступ к хосту; per-tenant auth и квоты — enforcement, а не «pairing в личке».
* **Skills, которые улучшаются под контролем.** Агент **предлагает** скиллы, но не меняет библиотеку молча: proposals проходят governance-review (ветка + diff + checks), а Skill Curator архивирует неиспользуемое и находит дубликаты. Самоулучшение без потери контроля.
* **Планировщик с замороженными правами (ADR-0019).** `svarog scheduler` исполняет джобы по интервалу/`daily_at`; создание агентом — неотключаемый critical-approval; ослабление конфига не повышает прав уже одобренной джобы.
* **Resumable по построению.** Run — state machine с checkpoint'ами: ожидание approval, refuel при переполнении контекста, падение процесса, лимит стоимости — ничто не теряет прогресс. Долгие задачи живут часами и днями.
* **Любой кодинг-агент как data-plane (ADR-0016).** Run может исполнять Claude Code / Codex / OpenCode внутри sandbox Svarog: LLM-трафик — через метерящий прокси (ключ провайдера не входит в sandbox), память/скиллы/approvals — через MCP-мост, policy — хуками. Зрелость внешнего агента + governance и аудит Svarog одновременно.
* **Один core — много клиентов.** CLI, REST/WebSocket, Web UI («Горн»), Telegram и remote-CLI cloud-режима (ADR-0017) гоняют один и тот же прогон задачи; approval можно выдать через любой канал.
* **Проверяемость важнее самооценки.** Детерминированный verifier (тесты, линтеры, secret scan) приоритетнее «я всё сделал» от модели; полный trace отвечает, почему выбран скилл, что запускалось и кто подтвердил. Регрессии поведения ловит agent-based user simulation (`simulation/`).
* **Минимум инфраструктуры, model-agnostic.** Только Git + SQLite; любой OpenAI-совместимый endpoint (vLLM, Ollama, llama.cpp, LiteLLM, OpenRouter, корпоративный); работает в закрытом контуре и air-gapped.

## Возможности

* **Интерфейсы**: CLI (`run`, `chat`, `resume`, `traces`, `sessions`, `approvals`, `skills`, `memory`, `cron`, `scheduler`, `secrets`, `tenant`, `login`, `remote`, `doctor`), REST + WebSocket API, Web UI («Горн», `web/`), Telegram-бот — с асинхронным approval из любого канала.
* **Инструменты**: файлы (read/write/edit/list/search в границах workspace), bash в sandbox, `update_plan` (run-local план для сложных задач), `read_skill`, `remember`, `read_memory`, `search_memory` (FTS5), `create_skill_proposal`, `propose_memory_change` (Dream), `schedule_task` (critical-approval), `request_approval`, `ask_user` (уточняющий вопрос человеку с таймаутом), плюс внешние инструменты через **MCP**. Git — не tool агента, а привилегированный host-flow вне sandbox (ADR-0002/0003).
* **Sandbox**: Docker (сеть off, non-root, лимиты CPU/RAM, timeout, mounts по allowlist) или явный `local-trusted`.
* **Policy Engine**: allow / notify / deny / require_approval с режимами автономии и правилами `policies/*.yaml`; неотключаемый critical-набор (продовый деплой, выдача секретов, force-push, ослабление политик, создание cron-джобы); в `chat` approval-гейт решается **live**, без suspend/resume.
* **Память — LLM-wiki (ADR-0011)**: страницы проектов с YAML-frontmatter, детерминированный автоген `index.md`/`log.md` в single-writer'е, прогрессивная загрузка (в контекст — только индекс + профиль, остальное по требованию через `read_memory`), FTS5-retrieval (`search_memory` + авто-инъекция при переполнении индекса), lint `svarog memory curate`, неизменяемый raw-слой `sources/`. Запись — только через контролируемую очередь (Flow A).
* **Гиперперсонализация (ADR-0021)**: типизированный `user/profile.md` → персона-директива в системном промпте; автозахват фактов после сессий (aux-модель, Flow A, без ревью); Dream — семантическая консолидация под human review (memory-proposals).
* **Планировщик (ADR-0019)**: отдельный демон `svarog scheduler`; джобы `every` / `daily_at`; права заморожены при создании; системные джобы — skill-куратор и Dream; агентское создание только через critical-approval.
* **Git-flows**: Flow B (скиллы через proposals), Flow C (рабочий код: pull → task-ветка → commit с secret scan → push через policy); memory-proposals (ADR-0020) — очередь в БД, не git-ветка.
* **Skill governance + Curator**: proposals с review, двухслойное кураторство (механический pruning + LLM-консолидация на auxiliary-модели).
* **Надёжность**: resumable runs, refuel, recovery после падения, бюджеты токенов/стоимости, полный audit trace в SQLite.
* **Секреты**: pluggable SecretStore (файл 0600 / env), инжекция только в sandbox, redaction в trace, обязательный secret scan перед каждым коммитом и push.
* **Мультиарендность (ADR-0012/0013/0014)**: изолированные тенанты — tenant = свой agent-home (память/скиллы/секреты/БД/workspace); роли **superuser** (доступ к хосту, `local-trusted`) и **standard** (принудительно docker-sandbox, без доступа к хосту и файлам других тенантов, fail-closed без docker); per-tenant auth (bearer-токен или JWT), квоты (одновременность + бюджеты cost/tokens → HTTP 429), провижн `svarog tenant …` и опциональный first-touch. Выключена по умолчанию (`tenancy.enabled`) — single-tenant поведение без изменений.
* **Внешние агенты как data-plane (ADR-0016)**: run может исполнять Claude Code / Codex / OpenCode внутри sandbox Svarog (`executor.type: external`) или делегировать им подзадачи из нативного цикла (`spawn_child_run`). Агент заперт в internal-only сети — наружу только relay к прокси Svarog: LLM-трафик метерится (бюджеты — enforcement → 429 → suspend), память/скиллы/approvals доступны только через MCP-мост. Claude Code — по API-ключу или **личной подписке Pro/Max** (OAuth-токен); для него же cooperative-tier: policy PreToolUse-хук и suspend/resume approvals. Образы агентов — в репозитории (`docker/agent-claude/`, `docker/agent-opencode/`); осиротевшие контейнеры подметает GC по PID владельца.
* **Cloud-режим (ADR-0017, фазы 1–2)**: постоянный сервер поверх `svarog serve` + thin CLI: `svarog login <url>` и `svarog remote run|chat|resume|cancel|runs|show|approvals|skills|whoami` — тонкий 1:1 маппинг на REST/NDJSON, локально не исполняется ничего. Серверные workspaces двух видов: git-клон по `--repo` (hardened clone, per-tenant git-credentials только host-side) и постоянный **named workspace** (`--workspace`), живущий между runs и сессиями; результаты — push task-ветки (Flow C), `GET /runs/{id}/diff` или `svarog remote workspace pull` (файл / tar.gz). Сессии `remote chat` держат **тёплый sandbox** (env, инфраструктура, MCP живут между сообщениями), одноразовые workspaces подметает retention-GC.
* **Тестирование агента**: **agent-based user simulation** (`simulation/`) — сценарии × личности для регрессионной проверки поведения агента на реальном LLM (какие tools, зацикливание, маршрутизация результата в файл/память); плюс полный набор unit-тестов и eval-сценарии критериев готовности MVP, гоняемые в CI.

Архитектурные решения за этими свойствами зафиксированы в [ADR-0001…0021](docs/adr/) (hardening рантайма — 0015, внешний агент как data-plane — 0016, cloud-режим — 0017, чат-TUI — 0018, планировщик — 0019, memory-proposals и Dream — 0020, гиперперсонализация — 0021).

## Режимы работы

Svarog работает в двух режимах, на одном и том же core (§10):

* **Локальный CLI** (готово) — `svarog` запускается прямо на вашей машине, как `claude`/`codex`: вы вызываете его из терминала, он исполняет run в своём sandbox и завершается. Один agent-home (память/скиллы/БД) может обслуживать запуски из любой рабочей папки — см. «Чтобы `svarog` работал из любой папки» в Quick Start.
* **Cloud-агент** (ADR-0017, фазы 1–2 реализованы) — постоянно работающий инстанс `svarog serve` на сервере: клиенты подключаются через `svarog login` / `svarog remote …`, run'ы исполняются на сервере в серверных workspaces, результаты забираются как diff, push или архив — см. «Cloud-режим». Остались admin-plane (управление тенантами по HTTP) и деплой-упаковка — см. «Дорожная карта».

## Использование

### Автономия и approvals

По умолчанию агент автономен (yolo-first, ADR-0010) — основной сценарий это работа без няньки. Approval требуется только для типизированного critical-набора (продовый деплой, выдача секретов, force-push, ослабление политик, создание cron-джобы) — и его **нельзя отключить конфигом**; всё остальное обратимо (ветки, коммиты, rollback) и видно в trace.

Approval-запросы (critical-действия, режим `--supervised`, tool `request_approval`) переводят run в `waiting_approval`. В терминале решение запрашивается сразу; в `chat` гейт решается **live прямо в диалоге** — run продолжает стриминг без suspend/resume; без TTY — через `svarog approvals`, затем `resume`. Вопрос агента `ask_user` работает так же: `svarog approvals answer <id> "текст"`.

Run — возобновляемый state machine (ADR-0005): при превышении лимитов итераций/токенов/стоимости он приостанавливается (`suspended`), а не падает — поднимите лимит в конфиге и выполните `svarog resume`.

### Скиллы и Curator

Скиллы (`skills/*/SKILL.md`, формат [agentskills.io](https://agentskills.io)) подгружаются карточками в контекст, полное содержимое — через `read_skill`. Прямые правки `skills/` запрещены policy — агент предлагает новый/обновлённый скилл tool'ом `create_skill_proposal` (Flow B, §18): заявка валидируется, материализуется в отдельной ветке skills-репозитория с secret scan, и человек смотрит diff (`svarog skills proposals show`) и решает merge/reject.

Skill Curator слой 1 (`svarog skills curate`, §18.1) по usage-статистике из trace переводит неиспользуемые agent-created скиллы active→stale→archived (обратимо); archived-скиллы не попадают в карточки контекста, `pinned` выводит скилл из-под авто-переходов. По интервалу тот же pruning запускает системная джоба планировщика (`curator.prune_interval_sec`, нужен `svarog scheduler`). Слой 2 (`--semantic`, opt-in, ADR-0009) прогоняет библиотеку через auxiliary-модель: находит дубликаты, предлагает улучшения описаний и архивацию, пишет отчёт в `artifacts/`; содержательные правки оформляются как skill proposals, а не применяются молча.

### Память, гиперперсонализация и Dream

Память (`memory/`, Flow A) читается в контекст, а обновляется агентом только через контролируемую очередь single writer'а (ADR-0004), сериализованную межпроцессным файловым локом — параллельные интерфейсы на одной машине не конфликтуют на git-репозитории памяти. Изменения кода идут по Flow C: pull → task-ветка → commit (с обязательным secret scan) → push через policy.

**Гиперперсонализация (ADR-0021)** работает из коробки. `user/profile.md` — типизированные H2-секции: поведенческие (`Тон`, `Язык`, `Предпочтения`, `Не трогать`) собираются в персона-директиву системного промпта; фактические (`Роль`, `Расписание`, …) остаются справочным контекстом. **Автозахват** (`autocapture.enabled: true` по умолчанию) после сессии извлекает долговечные факты aux-моделью и пишет их аддитивно через Flow A (без ревью: jail профиля, дедуп, откат через git). Выключается `autocapture.enabled: false`.

**FTS5-retrieval** (включён по умолчанию, `memory.fts_enabled`): индекс в runtime-БД, синхронизирует writer; агент ищет по содержимому tool'ом `search_memory`; при переполнении `index.md` релевантные страницы подмешиваются автоматически.

**Dream** — фоновая семантическая консолидация памяти (ADR-0020): ищет структурную гниль и смысловые противоречия и **предлагает** правки, писать в память сам не может (в профиле Dream нет `remember`/shell/файлов). Предложения ждут человека: `svarog memory proposals list | show <id> | approve <id> | reject <id>`; одобренное попадает в очередь Flow A и применяется при следующем `svarog memory flush` или в конце следующего run'а. Dream **включён по умолчанию** (ADR-0021): конфиг заводит системную джобу `system:memory-dream`; дальше — `svarog cron disable` / `enable`. Для качества консолидации `models.auxiliary` лучше указать способную модель (не самую дешёвую).

При длинных задачах срабатывает refuel: состояние пишется в `task_state.md`, run **приостанавливается** (освобождая процесс и sandbox), а затем поднимается с пересобранным контекстом — командой `svarog resume` для одноразового `run` либо автоматически refuel-supervisor'ом в `serve`/`telegram` (cross-process, ADR-0005; `refuel_after_iterations > max_iterations` отключает refuel).

### Планировщик

Джобы исполняет отдельный демон `svarog scheduler` (ADR-0019) — `svarog serve` их **не** тикает (один источник тика, без гонок за lease). Расписание: интервал (`--every SEC`) или время суток (`--at HH:MM`); полный cron-синтаксис вне объёма.

```bash
svarog cron add --task "обнови отчёт" --every 86400
svarog cron add --task "бэкап заметок" --at 09:00 --tz Europe/Moscow
svarog cron list / show <id> / enable|disable|remove <id>
svarog scheduler   # держать запущенным рядом с serve, если нужны джобы
```

Права джобы (автономия + дайджест security-конфига) замораживаются при создании; при срабатывании дайджест сверяется с текущим — расхождение отключает джобу (fail-closed, как у `resume`). Создание агентом (`schedule_task`) — в неотключаемом critical-наборе: нужен approval даже в yolo. Пропущенные тики при остановленном демоне **не** догоняются штормом — at-least-once без catch-up. Занятый workspace — пропуск тика, не ошибка.

Системные джобы (`protected`): механический skill-куратор по `curator.prune_interval_sec` и Dream (`dream.enabled`). Агент их не удаляет; выключенную человеком джобу код при рестарте не включает обратно.

### Верификация и секреты

После завершённого run детерминированный verifier прогоняет проверки (тесты, линтеры из `verifier.checks`, secret scan рабочего дерева и skill-specific checks) — упавшая проверка приоритетнее самооценки агента (§6.11) и даёт exit code 4. Секреты хранятся в SecretStore, агент видит только имена (`api_key_ref`); значения инжектируются в окружение sandbox только для явно перечисленных в `secrets.inject` и вырезаются (redaction) из trace и tool-выводов.

### Sandbox и policy

Bash-команды агента по умолчанию исполняются в **Docker sandbox** (сеть выключена, non-root, лимиты CPU/RAM — ADR-0002); нужен Docker или Podman. Без изоляции — явный режим `sandbox: {type: local-trusted}` в `svarog.yaml`.

Policy-правила проекта живут в `policies/*.yaml` и могут только ужесточать поведение:

```yaml
rules:
  - match: "file.*"          # fnmatch по типу операции (file.write, bash.exec, …)
    decision: deny           # deny | require_approval | notify
    reason: "инфраструктуру руками не трогаем"
    paths: ["infra/**"]      # опционально, по аргументу path
```

### Gateway, Telegram и MCP

Gateway (`svarog-harness[server]`, §10.4) поднимает тот же прогон задачи через HTTP: `POST /runs` создаёт run и возвращает `run_id`, `WS /runs/{id}/events` стримит текст/tool calls/checks/финал, `POST /approvals/{id}` принимает решение, а `POST /approvals/{id}/answer` — ответ на `ask_user`; оба асинхронно возобновляют run (ADR-0005). CLI и gateway используют один `TaskRunner`. В долгоживущем процессе работает refuel-supervisor: refuel-suspended run'ы поднимаются автоматически. При bind не на loopback (`--host 0.0.0.0`) обязателен bearer-token (`gateway.token_ref` → SecretStore). При `tenancy.enabled` gateway работает **мультиарендно** (ADR-0014): bearer/JWT-токен резолвится в тенанта, каждый run исполняется в изоляции своего agent-home; роль `standard` принудительно заперта в docker-sandbox, квоты дают HTTP 429.

Telegram-бот (§10.2) — тот же `GatewayService` поверх Bot API: сообщение порождает run, `waiting_approval` показывается inline-кнопками approve/deny, вопрос `ask_user` — текстом (следующее сообщение — ответ, `/skip` — продолжить без него). Токен бота — секрет (`telegram.token_ref`), доступ — allowlist `telegram.allowed_users` (§16); в мультиарендном режиме доступ определяется реестром (`telegram:<user_id>` → тенант), при `provisioning: first_touch` новый пользователь авто-провижнится.

MCP-серверы (`svarog-harness[mcp]`, §9) подключаются секцией `mcp.servers` в `svarog.yaml`: их инструменты проходят discovery и регистрируются как обычные tools, но по умолчанию получают `risk: high` и требуют approval (ADR-0010), пока администратор не ослабит их профилем `notify`. Токены серверов — секреты (`env_refs` → SecretStore).

Документы (`svarog-harness[docs]`) — MCP-tool `read_document` на мосте: внешний агент читает PDF/DOCX/XLSX/PPTX/HTML/EPUB/CSV из workspace как Markdown (конвертация markitdown на хосте). Без группы `docs` инструмент молча отсутствует, `svarog doctor` подскажет установку.

### Внешний агент как data-plane (ADR-0016)

Svarog может выполнять run не своим нативным циклом, а внешним кодинг-агентом (Claude Code / Codex / OpenCode) внутри собственного sandbox — оставаясь control-plane: прокси LLM с бюджетами, память/скиллы/approvals через MCP-мост, policy-хуки. Образы включают `pandoc`, `poppler-utils` (pdftotext/pdftoppm) и `tesseract` (rus/eng): агент конвертирует документы и OCR'ит сканы через bash; изображения модель видит через нативный `Read` (claude) или MCP-tool `read_image` моста (все агенты с MCP). Соберите образ агента (CLI не пиннится — всегда свежий, см. [`docker/agent-claude/`](docker/agent-claude/), [`docker/agent-opencode/`](docker/agent-opencode/)) и включите `executor: external`:

```bash
docker build -t svarog/agent-claude:latest docker/agent-claude
# всегда последний CLI вопреки кэшу слоёв:
docker build --build-arg REFRESH=$(date +%s) -t svarog/agent-claude:latest docker/agent-claude
```

```yaml
executor:
  type: external
  external:
    adapter: claude-code            # claude-code | codex | opencode
    image: svarog/agent-claude:latest
    auth: subscription              # subscription (Pro/Max, OAuth) | api-key
    oauth_token_ref: CLAUDE_OAUTH   # секрет со значением из `claude setup-token`
    enforcement: cooperative        # cooperative (policy-хуки + approvals) | containment
```

Для подписки положите OAuth-токен в SecretStore под именем из `oauth_token_ref` (`svarog secrets set CLAUDE_OAUTH`, значение — из `claude setup-token`); расход идёт против вашего плана Pro/Max, ключ провайдера в sandbox не попадает. Для API-ключа — `auth: api-key` + `api_key_ref` (ключ инжектируется на прокси, не в контейнер). Дальше — обычный `svarog run` / `chat`: агент работает в изоляции Svarog, память сохраняется через `mcp__svarog__remember`, push по-прежнему только через `svarog push` с policy. Cooperative-tier (policy-хуки + suspend/resume approvals) и MCP-инструменты доступны только для `claude-code`; с `codex`/`opencode` supervised и память/скиллы недоступны (containment-only, fail-closed).

### Cloud-режим (ADR-0017)

Cloud-режим — это deployment-профиль `svarog serve`, а не отдельная подсистема: тот же gateway и мультиарендность, дополненные серверными workspaces и thin CLI. Сценарий — self-hosted сервер команды: админ провижнит тенантов (`svarog tenant …`), пользователи подключаются удалённо и не исполняют локально **ничего**:

```bash
svarog login https://svarog.team.example   # сохранит URL в ~/.svarog/svarog.yaml, токен — в SecretStore
svarog remote whoami                        # тенант, роль, активные runs, usage
svarog remote run "почини CI" --repo git@github.com:team/app.git --ref main
svarog remote run "собери отчёт" -w reports # run в постоянном named workspace
svarog remote chat -w reports               # сессия чата в том же workspace
svarog remote runs / show / resume / cancel <id>
svarog remote approvals / approve / deny <id>
svarog remote workspace create|list|rm|pull # lifecycle named workspaces
```

Workspace на сервере берётся из двух источников (ADR-0017 §1). **Git-клон** (`--repo`): сервер клонирует репозиторий host-side hardened-флагами в одноразовый task-workspace, дальше штатный Flow C — task-ветка, guarded commit, push по policy; git-credentials — per-tenant секрет (`git.credentials`), резолвится только на хосте и никогда не попадает в sandbox или trace. **Named workspace** (`--workspace <name>`): постоянный каталог тенанта, живущий между runs и сессиями — агент накапливает в нём результаты; параллельный run в занятом workspace получает 409, retention-GC его не трогает. Результаты забираются push'ем task-ветки, диффом (`GET /runs/{id}/diff`) или `svarog remote workspace pull <name> [path]` (файл или tar.gz-архив).

`remote chat` создаёт сессию (сообщение = run, workspace общий на сессию) и решает approvals прямо из чата. Сессии держат **тёплый sandbox**: env, инфраструктура (bridge/сеть/relay) и MCP-бэкенды живут между сообщениями (idle-GC по `cloud.warm_session_ttl_sec`, по умолчанию 900 с) — без старта контейнера на каждое сообщение. Всё это работает и с внешними executor'ами (ADR-0016 × ADR-0017). Admin-plane (`/admin/*` — управление тенантами без shell-доступа) и деплой-упаковка — фаза 3, см. «Дорожная карта».

## Сравнение с Hermes и OpenClaw

Три разных продукта под разные задачи: Svarog — **платформа/runtime для сборки агента**, Hermes — готовый широкий агент, OpenClaw — зрелый персональный ассистент. Все три self-hosted.

**Hermes** ([NousResearch/hermes-agent](https://github.com/NousResearch)) — зрелый production-агент на Python и один из референсов Svarog: из него перенята идея двухслойного Skill Curator, заморозка автономии при старте run и эвристики опасных bash-команд. Hermes сегодня шире по охвату каналов (gateway на 6 платформ, subagents, компакция контекста). Svarog отличается не объёмом фич, а backbone: Git-native память с тремя flow и single-writer'ом, security-through-enforcement, resumable-first loop, изоляция арендаторов, внешний агент как data-plane, планировщик с замороженными правами.

**OpenClaw** ([openclaw/openclaw](https://github.com/openclaw/openclaw)) — очень популярный self-hosted персональный ассистент на TypeScript/Node: голос, Canvas, мобильные ноды, 20+ каналов, реестр скиллов ClawHub. По охвату каналов, зрелости и DX Svarog ему сильно уступает; отличие — строгий backbone: версионируемая Git-память, governance скиллов с review, формальный Policy Engine, resumable state machine, полноценные арендаторы вместо pairing в DM.

| | **Svarog** | **Hermes** | **OpenClaw** |
|---|---|---|---|
| Позиционирование | платформа/runtime для сборки агента | готовый широкий агент | зрелый персональный ассистент |
| Стек | Python | Python (монолит) | TypeScript/Node |
| Долгосрочная память | **Git-native, 3 flow, single-writer, FTS5, гиперперсонализация + Dream** | provider-модель + FTS5 по сессиям | workspace-файлы + сессии |
| Скиллы | `SKILL.md` + **governance + Curator** | agentskills.io + Curator | `SKILL.md` + реестр ClawHub |
| Безопасность | **enforcement-инварианты + prompt-injection hardening** | эвристики + smart approval | sandbox + pairing DM |
| Изоляция арендаторов | **per-tenant agent-home + роли + квоты** | subagents (в пределах процесса) | pairing-политика DM |
| Автономия | yolo-first + **неотключаемый critical-набор** | YOLO с заморозкой | pairing / approval незнакомцев |
| Resumability | **state machine + checkpoints (основа)** | checkpoints | сессии |
| Планировщик | **отдельный демон, права заморожены, critical-approval (ADR-0019)** | cron внутри агента | cron / heartbeat |
| Внешний агент как data-plane | **Claude Code/Codex/OpenCode в sandbox + прокси/MCP/policy (ADR-0016)** | subagents (в пределах процесса) | Docker/SSH для не-основных сессий |
| Удалённый режим | **cloud-режим: серверные workspaces + thin CLI (ADR-0017)** | gateway-платформы | Node-gateway |
| Интерфейсы / охват | CLI + REST/WS + Web UI + Telegram + remote-CLI (один core) | 6 платформ | **20+ каналов, голос, mobile** |
| Инфраструктура | **только Git + SQLite** | монолит | Node-gateway + workspace |
| Зрелость / комьюнити | pre-alpha | **production** | **очень зрелый, большое сообщество** |

**Что выбрать:**

- **Hermes** — нужен широкий готовый агент на много мессенджеров уже сейчас.
- **OpenClaw** — нужен зрелый персональный ассистент с голосом/мобильным на 20+ каналов.
- **Svarog** — нужна аудируемая **платформа**: версионируемая Git-память, security-through-enforcement, resumable-first, изоляция арендаторов и минимум инфраструктуры (Git + SQLite), а не готовый ассистент.

## Документация

| Документ | Содержание |
|---|---|
| [TASK.md](TASK.md) | полное ТЗ |
| [docs/adr/](docs/adr/) | архитектурные решения ADR-0001…0021 (мультиарендность — 0012/0013/0014; hardening рантайма — 0015; внешний агент как data-plane — 0016; cloud-режим — 0017; чат-TUI — 0018; планировщик — 0019; memory-proposals и Dream — 0020; гиперперсонализация — 0021) |
| [docker/agent-claude/](docker/agent-claude/) | образ sandbox для внешнего Claude Code (ADR-0016) + инструкция сборки |
| [docker/agent-opencode/](docker/agent-opencode/) | образ sandbox для внешнего OpenCode (ADR-0016) + инструкция сборки |
| [web/](web/) | Web UI «Горн» (чат, настройки, память, скиллы, трейс) поверх REST/WS |
| [docs/repo-structure.md](docs/repo-structure.md) | структура пакета |
| [docs/first-issues.md](docs/first-issues.md) | backlog M0–M5 |
| [AGENTS.md](AGENTS.md) | правила работы с репозиторием |
| [simulation/](simulation/) | agent-based user simulation: инструкции, сценарии, личности для тестирования Svarog на реальном LLM |

## Разработка

```bash
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest          # юнит-тесты
uv run pytest evals    # eval-сценарии критериев готовности MVP (§26)
```

`evals/` — исполняемые сценарии критериев готовности MVP (§26, ADR-0008): init agent-home, run задачи с файлами, bash в sandbox, approval-гейт, полнота trace, refuel, resume после «падения процесса». Прогоняются через настоящий стек с scripted-LLM (без сети) и запускаются в CI.

## Дорожная карта

Куда движется проект — по двум осям.

### Продукт и бизнес-логика

* **Cloud-режим: фаза 3+ (ADR-0017)** — фундамент готов (серверные workspaces, diff/resume/cancel API, sessions с тёплым sandbox, thin CLI `svarog login`/`remote` — фазы 1–2); остаются admin-plane (`/admin/*` — управление тенантами без shell-доступа к хосту) и деплой-упаковка (reverse-proxy/TLS, docker compose).
* **Web UI** (§10.3) — веб-клиент «Горн» (`web/`) сделан поверх REST/WS gateway: чат с выбором исполнителя/провайдера/модели, слэш-командами, `@`-автодополнением файлов и вложениями (скриншот, PDF, docx, xlsx — вставкой или перетаскиванием), настройки, память, скиллы, трейс и дифф запусков; остаются approval inbox отдельным экраном, skill proposals/curate и светлая тема. «Человеческие» UI сегодня — CLI, Telegram и веб.
* **Гранулярный RBAC** (§16) — базовая мультиарендность уже реализована (ADR-0012/0013/0014); остаётся детальный RBAC внутри тенанта — роли owner/admin/developer/operator/viewer/agent (право approve, редактирование policies) — и scale-бэкенд (shared-Postgres с `tenant_id` вместо N SQLite).
* **Semantic retrieval** (Vector DB, §6.7/§14) — FTS5 по памяти уже в runtime (без внешних сервисов); дальше — embeddings/Qdrant для семантического поиска по памяти, скиллам и документам и ускорения Curator слоя 2.
* **LLM-as-judge verifier** (§6.11) — качественная оценка результата вторым LLM-вызовом поверх детерминированных проверок (которые остаются приоритетными).
* **История/compaction контекста** (§6.3) — сжатие диалога внутри одного run (сейчас роль compaction выполняет только refuel между run'ами).
* **Расширение sandbox** (§6.9) — network allowlist, Kubernetes Job / remote runner, gVisor/Firecracker, air-gapped режим.
* **Готовые official skills** (§23) — `git-workflow`, `python-project`, `fastapi-service`, `report-writer`, `skill-curator` и др. с примерами и checks.
* **Retention policy для trace** (§6.12) — авто-очистка сырых tool-выводов через N дней при сохранении метаданных и approvals.
* **Cost/observability дашборд** — метрики стоимости, длительности, token usage по runs/сессиям.

### Оформление и developer experience

* **Публикация на PyPI** — `svarog-harness` под extras `[server]`/`[mcp]`; сейчас установка только из репозитория.
* **Документация сайтом** — MkDocs/Sphinx: гайды по установке, скиллам, policy, интерфейсам (вместо чтения `TASK.md`).
* **CONTRIBUTING.md + шаблоны issue/PR** — для внешних контрибьюторов.
* **Docker Compose / dev-container** — запуск gateway + модели одной командой (§24 переносимость).
* **Скринкаст/GIF** быстрого старта в README и примеры agent-home.
* **OpenAPI-схема и клиентский пример** для gateway (FastAPI уже отдаёт `/docs`).

## Лицензия

[Apache-2.0](LICENSE)
