<p align="center">
  <img src="assets/logo.png" alt="Svarog Agent Harness" width="480">
</p>

<h1 align="center">Svarog Agent Harness</h1>

**Svarog** — open-source, self-hosted, Git-native runtime для ИИ-агентов: скиллы, sandboxed execution, Git-память, refuel loops, approval policies и полный audit trace. Это платформа для сборки агентов, а не готовый агент и не workflow-фреймворк.

> Pre-alpha, но работает end-to-end и разворачивается self-hosted. Публичного контракта API пока нет — детали могут меняться.

## Чем Svarog отличается

Большинство agent-платформ — это либо конструкторы графов и воркфлоу, либо облачные ассистенты с непрозрачной памятью и полным доступом к системе. Svarog устроен иначе:

* **Harness, а не граф.** Вы не проектируете дерево поведения. Агент работает циклом `observe → reason → select skill/tool → act → verify` в контролируемой среде. Графы и state machines — опция, а не фундамент.
* **Git-native память вместо чёрного ящика.** Память, решения, задачи и артефакты — человекочитаемые markdown-файлы в Git: их ревьюят, версионируют, откатывают и переносят между машинами. Не «эмбеддинги, которым остаётся доверять», а обычный `git log`. Запись идёт через single-writer очередь, поэтому несколько интерфейсов и параллельных run'ов не создают merge-конфликтов.
* **Skills, которые улучшаются под контролем.** Скилл — это переиспользуемый пакет знаний и скриптов (формат [agentskills.io](https://agentskills.io)), а не просто вызов функции. В контекст модели идут только краткие карточки, полное содержимое — по требованию. Агент может **предлагать** новые скиллы, но не менять библиотеку молча: изменения проходят governance-review (ветка + diff + checks), а **Skill Curator** сам поддерживает библиотеку в форме — архивирует неиспользуемое и на LLM-слое находит дубликаты и улучшает описания. Это делает агента самоулучшающимся без потери контроля.
* **Безопасность через enforcement, а не «распознавание опасных команд».** LLM считается недоверенным компонентом. Гарантии дают инварианты: sandbox с выключенной сетью, non-root и без доступа к секретам; секреты живут только как **именованные ссылки** и вырезаются из контекста и trace; режим автономии и policy **замораживаются при старте run** — инъекция из файла или документа не может повысить права.
* **Yolo-first, но с неотключаемым тормозом.** По умолчанию агент автономен (основной сценарий — работа без няньки). Approval требуется только для типизированного critical-набора (продовый деплой, выдача секретов, force-push, ослабление политик) — и его нельзя отключить конфигом. Всё остальное — обратимо (ветки, коммиты, rollback) и видно в trace.
* **Resumable по построению.** Run — это state machine с checkpoint'ами. Ожидание approval, refuel при переполнении контекста, падение процесса, лимит стоимости — ничто не теряет прогресс; run поднимается с последнего шага. Долгие задачи живут часами и днями.
* **Model-agnostic и минимум инфраструктуры.** Self-hosted, как и Hermes с OpenClaw, — но без обязательных внешних сервисов: нужны только Git и SQLite. Любой OpenAI-совместимый endpoint (vLLM, Ollama, llama.cpp, LiteLLM, OpenRouter, корпоративный); работает в закрытом контуре и air-gapped.
* **Один core — много интерфейсов.** CLI, REST/WebSocket и Telegram гоняют один и тот же прогон задачи; approval можно выдать через любой канал, а не только там, где запустили.
* **Проверяемость важнее самооценки.** Детерминированный verifier (тесты, линтеры, secret scan) имеет приоритет над «я всё сделал» от модели, а полный trace отвечает на вопросы «почему выбран этот скилл», «что запускалось», «кто подтвердил».

## Возможности

* **Интерфейсы**: CLI (`run`, `chat`, `resume`, `traces`, `approvals`, `skills`, `memory`, `secrets`), REST + WebSocket API, Telegram-бот — с асинхронным approval.
* **Инструменты**: файлы (read/write/edit/list/search в границах workspace), bash в sandbox, git, `read_skill`, `remember`, `create_skill_proposal`, `request_approval`, плюс внешние инструменты через **MCP**.
* **Sandbox**: Docker (сеть off, non-root, лимиты CPU/RAM, timeout, mounts по allowlist) или явный `local-trusted`.
* **Policy Engine**: allow / notify / deny / require_approval с режимами автономии и правилами `policies/*.yaml`; MCP-инструменты по умолчанию требуют approval.
* **Память и Git**: Flow A (память, single-writer), Flow B (скиллы через proposals), Flow C (рабочий код: pull → task-ветка → commit с secret scan → push через policy).
* **Skill governance + Curator**: proposals с review, двухслойное кураторство (механический pruning + LLM-консолидация на auxiliary-модели).
* **Надёжность**: resumable runs, refuel, recovery после падения, бюджеты токенов/стоимости, полный audit trace в SQLite.
* **Секреты**: pluggable SecretStore (файл 0600 / env), инжекция только в sandbox, redaction в trace, обязательный secret scan перед каждым коммитом и push.

Архитектурные решения за этими свойствами зафиксированы в [ADR-0001…0010](docs/adr/).

## Сравнение с Hermes и OpenClaw

Все три — self-hosted, поэтому различие не в этом. Ниже честно, где Svarog отличается, а где проигрывает.

**Hermes** ([NousResearch/hermes-agent](https://github.com/NousResearch)) — зрелый production-агент на Python и один из референсов Svarog: из него перенята сама идея двухслойного Skill Curator, паттерн заморозки автономии при старте run и эвристики опасных bash-команд. Hermes сегодня **шире**: gateway на 6 платформ (Telegram/Discord/Slack/WhatsApp/Signal/CLI), subagents, cron-планировщик, компакция контекста, code-execution RPC, батч-генерация трасс. Svarog отличается архитектурой, а не объёмом фич: **Git-native память с тремя явно разделёнными flow** (память / скиллы / рабочий код) и single-writer очередью вместо monolithic-состояния; **security-through-enforcement** как основа (инварианты sandbox, секреты только именованными ссылками, secret scan перед каждым коммитом); **resumable-first** loop и решения, задокументированные в ADR.

**OpenClaw** ([openclaw/openclaw](https://github.com/openclaw/openclaw)) — очень популярный self-hosted персональный ассистент на TypeScript/Node: голос, Canvas, ноды для macOS/iOS/Android и охват **20+ каналов** (WhatsApp, Telegram, Slack, Discord, Signal, iMessage, Matrix, WeChat и др.). У него есть скиллы (`SKILL.md` + реестр ClawHub), sandbox (Docker/SSH) для не-основных сессий и pairing-политика доступа в DM. По охвату каналов, зрелости и DX Svarog ему сильно уступает. Отличие Svarog — не «ассистент на все мессенджеры», а **runtime/платформа для сборки агента** с более строгим backbone: Git-native версионируемая память с тремя flow (у OpenClaw — workspace-файлы и сессии), **governance для скиллов** (proposals + review, а не только реестр) и **Curator**, формальный Policy Engine с типизированным critical-набором и enforcement-инвариантами, resumable state machine с checkpoint'ами, а обязательная инфраструктура — только Git + SQLite.

| | **Svarog** | **Hermes** | **OpenClaw** |
|---|---|---|---|
| Тип | платформа/runtime для агентов | готовый агент | персональный ассистент |
| Стек | Python | Python (монолит) | TypeScript/Node |
| Долгосрочная память | Git-native, 3 flow, single-writer | provider-модель + FTS5 по сессиям | workspace-файлы + сессии |
| Скиллы | `SKILL.md` + governance + Curator | agentskills.io + Curator | `SKILL.md` + реестр ClawHub |
| Безопасность | enforcement + prompt-injection hardening | эвристики + smart approval | sandbox + pairing-политика DM |
| Автономия | yolo-first + неотключаемый critical-набор | YOLO с заморозкой | pairing / approval незнакомцев |
| Resumability | state machine + checkpoints (основа) | checkpoints | сессии |
| Интерфейсы | CLI + REST/WS + Telegram (один core) | 6 платформ | 20+ каналов, голос, Canvas, mobile |
| Инфраструктура | только Git + SQLite | монолит | Node-gateway, workspace |
| Зрелость | pre-alpha | production | очень зрелый, большое сообщество |

Итого: за охватом каналов и зрелостью — в OpenClaw, за широтой фич готового агента — в Hermes; Svarog выбирают, когда нужна аудируемая **платформа** с Git-native памятью, контролем безопасности и управляемым самоулучшением, а не готовый ассистент.

## Установка (для разработки)

```bash
git clone git@github.com:kravtandr/Svarog-Agent-Harness.git
cd Svarog-Agent-Harness
uv sync
uv run svarog version
```

Требования: Python 3.12+ (uv поставит сам), [uv](https://docs.astral.sh/uv/).

## Быстрый старт

Проще всего развернуть agent-home одной командой:

```bash
uv run svarog init                # интерактивно: путь, модель, base_url, ключ
```

`init` без флагов спрашивает каталог agent-home (по умолчанию `./agent-home` внутри проекта), имя модели, `base_url` endpoint и API-ключ. Он создаёт skills, memory (Flow A), policies, `.gitignore` для секретов; если agent-home лежит внутри проекта — добавляет его в `.gitignore` проекта (данные агента и секреты не попадут во внешний репозиторий). Введённый ключ **не** записывается в `svarog.yaml` — он сохраняется в SecretStore, а в конфиг попадает только имя (`api_key_ref`).

Без интерактива всё задаётся флагами:

```bash
uv run svarog init ./agent-home --no-input \
  --model qwen3-coder --base-url http://localhost:8000/v1 --api-key sk-…
```

Либо создайте `svarog.yaml` в рабочей директории вручную (полная схема — §13 [TASK.md](TASK.md)):

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

### Настройка API-ключа

Ключ модели **никогда не хранится в `svarog.yaml`** (схема провайдера строгая — `extra="forbid"`, поля под значение ключа нет). В конфиге указывается лишь имя секрета через `api_key_ref`, а значение резолвится на execution-слое через SecretStore (ADR-0006). Для локальных серверов (vLLM, llama.cpp) ключ не нужен — оставьте `api_key_ref` закомментированным, подставится заглушка.

```yaml
models:
  providers:
    local:
      base_url: https://openrouter.ai/api/v1
      model: qwen3-coder
      api_key_ref: PROVIDER_API_KEY   # имя секрета, не сам ключ
```

Само значение задаётся одним из двух способов (первым срабатывает файл, затем env):

```bash
# 1) файл секретов (FileSecretStore, ~/.svarog/secrets.json, права 0600)
uv run svarog secrets set PROVIDER_API_KEY      # спросит значение, не покажет в истории
uv run svarog secrets list                      # только имена, без значений

# 2) переменная окружения (EnvSecretStore) — имя должно совпадать с api_key_ref
export PROVIDER_API_KEY="sk-…"
```

> `.env` **не подхватывается автоматически** (нет `load_dotenv`). Если держите ключ в `.env`, подгрузите его в окружение вручную: `set -a; source .env; set +a`. Файлы `.env`, `*.key`, `.svarog/secrets*` и т.п. уже в `.gitignore` (denylist ADR-0006) и отвергаются git-flow до коммита.

Имя переменной произвольно — важно лишь, чтобы `api_key_ref` совпадал с реальным именем в файле/окружении. Если `api_key_ref` задан, а секрет не найден, run падает с `ApiKeyError` и подсказкой.

Затем:

```bash
uv run svarog run "создай hello.py, который печатает время" # выполнить задачу
uv run svarog traces list                                   # последние runs
uv run svarog traces show <run-id>                          # полный trace run'а
uv run svarog resume <run-id>                               # продолжить приостановленный run
uv run svarog approvals list                                # ожидающие подтверждения
uv run svarog approvals approve <id>                        # или deny <id> --reason "…"
uv run svarog skills list                                   # доступные скиллы и карточки
uv run svarog skills proposals list                         # skill proposals на review (Flow B)
uv run svarog skills proposals approve <id>                 # влить proposal (или reject <id>)
uv run svarog skills curate                                 # Curator слой 1: lifecycle по usage
uv run svarog skills curate --semantic                      # + слой 2: LLM-консолидация (opt-in)
uv run svarog skills pin <name>                             # закрепить скилл (вне авто-переходов)
uv run svarog memory show                                   # память, как она попадёт в контекст
uv run svarog push <branch>                                 # push task-ветки (Flow C, с policy)
uv run svarog chat                                          # интерактивная сессия (диалог из нескольких runs)
uv run svarog secrets set PROVIDER_API_KEY                  # записать секрет в файл store (0600)
uv run svarog serve                                         # REST/WebSocket gateway (extra `server`, §10.4)
uv run svarog telegram                                      # Telegram-бот (§10.2)
uv run svarog mcp list                                      # инструменты MCP-серверов (extra `mcp`, §9)
```

MCP-серверы (`svarog-harness[mcp]`, §9) подключаются секцией `mcp.servers` в `svarog.yaml`: их инструменты проходят discovery и регистрируются как обычные tools, но по умолчанию получают `risk: high` и требуют approval (§9, ADR-0010), пока администратор не ослабит их профилем `notify`. Токены серверов — секреты (`env_refs` → SecretStore), не значения в конфиге.

Gateway (`svarog-harness[server]`, §10.4) поднимает тот же прогон задачи через HTTP: `POST /runs` создаёт run и сразу возвращает `run_id`, `WS /runs/{id}/events` стримит текст/tool calls/checks/финал, `POST /approvals/{id}` принимает решение и асинхронно возобновляет run (ADR-0005). CLI и gateway используют один `TaskRunner`, поэтому логика агента не дублируется.

Telegram-бот (§10.2) — тот же `GatewayService` поверх Bot API: сообщение порождает run, ход прогона идёт в чат, `waiting_approval` показывается inline-кнопками approve/deny. Токен бота — секрет (`telegram.token_ref` → SecretStore, ADR-0006), доступ ограничен allowlist'ом `telegram.allowed_users` (§16).

После завершённого run детерминированный verifier прогоняет проверки (тесты, линтеры из `verifier.checks`, secret scan рабочего дерева и skill-specific checks) — упавшая проверка приоритетнее самооценки агента (§6.11) и даёт exit code 4. Секреты хранятся в SecretStore (файл `~/.svarog/secrets.json` с правами 0600 или env), агент видит только имена (`api_key_ref`); значения инжектируются в окружение sandbox только для явно перечисленных в `secrets.inject` и вырезаются (redaction) из trace и tool-выводов.

Скиллы (`skills/*/SKILL.md`, формат [agentskills.io](https://agentskills.io)) подгружаются карточками в контекст, полное содержимое — через `read_skill`. Прямые правки `skills/` запрещены policy — агент предлагает новый/обновлённый скилл tool'ом `create_skill_proposal` (Flow B, §18): заявка валидируется, материализуется в отдельной ветке skills-репозитория с secret scan, и человек смотрит diff (`svarog skills proposals show`) и решает merge/reject. Skill Curator слой 1 (`svarog skills curate`, §18.1) по usage-статистике из trace переводит неиспользуемые agent-created скиллы active→stale→archived (обратимо, без proposals); archived-скиллы не попадают в карточки контекста, `pinned` выводит скилл из-под авто-переходов. Слой 2 (`--semantic`, opt-in, ADR-0009) прогоняет библиотеку через auxiliary-модель: находит дубликаты, предлагает улучшения описаний и архивацию, пишет отчёт в `artifacts/`; содержательные правки (например, новое описание) оформляются как skill proposals, а не применяются молча. Память (`memory/`, Flow A) обновляется агентом только через контролируемую очередь single writer'а (ADR-0004), читается в контекст. Изменения кода идут по Flow C: pull → task-ветка → commit (с обязательным secret scan) → push через policy. При длинных задачах срабатывает refuel: состояние пишется в `task_state.md`, контекст пересобирается.

Bash-команды агента по умолчанию исполняются в **Docker sandbox** (сеть выключена, non-root, лимиты CPU/RAM — ADR-0002); нужен установленный Docker или Podman. Без изоляции — явный режим `sandbox: {type: local-trusted}` в `svarog.yaml`.

Run — возобновляемый state machine (ADR-0005): при превышении лимитов итераций/токенов/стоимости он приостанавливается (`suspended`), а не падает — поднимите лимит в конфиге и выполните `svarog resume`. Approval-запросы (critical-действия, режим `--supervised`, tool `request_approval`) переводят run в `waiting_approval`: в терминале решение запрашивается сразу, без TTY — через `svarog approvals`, затем `resume`.

Policy-правила проекта живут в `policies/*.yaml` и могут только ужесточать поведение:

```yaml
rules:
  - match: "file.*"          # fnmatch по типу операции (file.write, bash.exec, …)
    decision: deny           # deny | require_approval | notify
    reason: "инфраструктуру руками не трогаем"
    paths: ["infra/**"]      # опционально, по аргументу path
```

## Документация

| Документ | Содержание |
|---|---|
| [TASK.md](TASK.md) | полное ТЗ |
| [docs/adr/](docs/adr/) | архитектурные решения ADR-0001…0010 |
| [docs/repo-structure.md](docs/repo-structure.md) | структура пакета |
| [docs/first-issues.md](docs/first-issues.md) | backlog M0–M5 |
| [AGENTS.md](AGENTS.md) | правила работы с репозиторием |

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

* **Web UI** (§10.3) — trace viewer, approval inbox, skill browser, diff viewer, memory browser поверх уже готового REST/WS gateway. Сейчас единственный «человеческий» UI — CLI и Telegram.
* **RBAC и multi-user** (§16) — роли owner/admin/developer/operator/viewer/agent: доступ к workspace, право approve, редактирование policies. Нужно для server-развёртывания и корпоративного контура.
* **Semantic retrieval** (Vector DB, §6.7/§14) — Qdrant-backend для памяти, скиллов и документов; ускоряет и Curator слой 2 (сейчас он сравнивает пары LLM-ом без embeddings).
* **LLM-as-judge verifier** (§6.11) — качественная оценка результата вторым LLM-вызовом поверх детерминированных проверок (которые остаются приоритетными).
* **История/compaction контекста** (§6.3) — сжатие диалога внутри одного run (сейчас роль compaction выполняет только refuel между run'ами).
* **Расширение sandbox** (§6.9) — network allowlist, Kubernetes Job / remote runner, gVisor/Firecracker, air-gapped режим.
* **Готовые official skills** (§23) — `git-workflow`, `python-project`, `fastapi-service`, `report-writer`, `skill-curator` и др. с примерами и checks.
* **Retention policy для trace** (§6.12) — авто-очистка сырых tool-выводов через N дней при сохранении метаданных и approvals.
* **Cost/observability дашборд** — метрики стоимости, длительности, token usage по runs/сессиям.

### Оформление и developer experience

* **Публикация на PyPI** — `svarog-harness` под extras `[server]`/`[mcp]`; сейчас установка только из репозитория.
* **Документация сайтом** — MkDocs/Sphinx: гайды по установке, скиллам, policy, интерфейсам (вместо чтения `TASK.md`).
* **CONTRIBUTING.md + шаблоны issue/PR** — для внешних контрибьюторов; badges (CI, лицензия, версия) в README.
* **Docker Compose / dev-container** — запуск gateway + модели одной командой (§24 переносимость).
* **Скринкаст/GIF** быстрого старта в README и примеры agent-home.
* **OpenAPI-схема и клиентский пример** для gateway (FastAPI уже отдаёт `/docs`).

## Лицензия

[Apache-2.0](LICENSE)
