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
```

В M1 команды выполняются в режиме local-trusted (без изоляции); Docker sandbox и policy engine — M2 (см. backlog).

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
