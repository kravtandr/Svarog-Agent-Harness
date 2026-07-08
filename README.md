# Svarog Agent Harness

**Svarog** — open-source, self-hosted, Git-native runtime для ИИ-агентов: скиллы, sandboxed execution, Git-память, refuel loops, approval policies и полный audit trace. Это платформа для сборки агентов, а не готовый агент и не workflow-фреймворк.

> Статус: активная разработка, pre-alpha. API нестабилен.

## Ключевые идеи

* **Git-native**: память, скиллы и артефакты агента живут в Git-репозиториях — версионируемо, переносимо, ревьюится людьми.
* **Enforcement over classification**: безопасность обеспечивают инварианты sandbox (сеть выключена, mounts по allowlist, non-root, без секретов), а не классификация команд (ADR-0002).
* **Yolo-first**: агент автономен по умолчанию; подтверждение человека — только для неотключаемого типизированного critical-набора действий (ADR-0010).
* **Resumable runs**: run — state machine с checkpoint'ами; approval, refuel и рестарты не теряют прогресс (ADR-0005).
* **Только Git + SQLite обязательны**: Redis, Qdrant, Postgres — опциональные backends (ADR-0007).

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
uv run svarog init ~/agent-home   # skills, memory (Flow A), policies, .gitignore для секретов
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
uv run svarog memory show                                   # память, как она попадёт в контекст
uv run svarog push <branch>                                 # push task-ветки (Flow C, с policy)
uv run svarog chat                                          # интерактивная сессия (диалог из нескольких runs)
uv run svarog secrets set PROVIDER_API_KEY                  # записать секрет в файл store (0600)
uv run svarog serve                                         # REST/WebSocket gateway (extra `server`, §10.4)
uv run svarog telegram                                      # Telegram-бот (§10.2)
```

Gateway (`svarog-harness[server]`, §10.4) поднимает тот же прогон задачи через HTTP: `POST /runs` создаёт run и сразу возвращает `run_id`, `WS /runs/{id}/events` стримит текст/tool calls/checks/финал, `POST /approvals/{id}` принимает решение и асинхронно возобновляет run (ADR-0005). CLI и gateway используют один `TaskRunner`, поэтому логика агента не дублируется.

Telegram-бот (§10.2) — тот же `GatewayService` поверх Bot API: сообщение порождает run, ход прогона идёт в чат, `waiting_approval` показывается inline-кнопками approve/deny. Токен бота — секрет (`telegram.token_ref` → SecretStore, ADR-0006), доступ ограничен allowlist'ом `telegram.allowed_users` (§16).

После завершённого run детерминированный verifier прогоняет проверки (тесты, линтеры из `verifier.checks`, secret scan рабочего дерева и skill-specific checks) — упавшая проверка приоритетнее самооценки агента (§6.11) и даёт exit code 4. Секреты хранятся в SecretStore (файл `~/.svarog/secrets.json` с правами 0600 или env), агент видит только имена (`api_key_ref`); значения инжектируются в окружение sandbox только для явно перечисленных в `secrets.inject` и вырезаются (redaction) из trace и tool-выводов.

Скиллы (`skills/*/SKILL.md`, формат [agentskills.io](https://agentskills.io)) подгружаются карточками в контекст, полное содержимое — через `read_skill`. Прямые правки `skills/` запрещены policy — агент предлагает новый/обновлённый скилл tool'ом `create_skill_proposal` (Flow B, §18): заявка валидируется, материализуется в отдельной ветке skills-репозитория с secret scan, и человек смотрит diff (`svarog skills proposals show`) и решает merge/reject. Память (`memory/`, Flow A) обновляется агентом только через контролируемую очередь single writer'а (ADR-0004), читается в контекст. Изменения кода идут по Flow C: pull → task-ветка → commit (с обязательным secret scan) → push через policy. При длинных задачах срабатывает refuel: состояние пишется в `task_state.md`, контекст пересобирается.

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

## Лицензия

[Apache-2.0](LICENSE)
