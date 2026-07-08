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

Создайте `svarog.yaml` в рабочей директории (полная схема — §13 [TASK.md](TASK.md)):

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
```

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
uv run pytest
```

## Лицензия

[Apache-2.0](LICENSE)
