# Sandbox-образ агента Claude Code (ADR-0016)

Образ data-plane для `executor: external` с `adapter: claude-code`. Внутри —
Node (сам агент `claude`), Python (hook-скрипт tier 2), git (Flow C).

## Сборка

`svarog init` собирает образ автоматически, когда настроен Claude Code. Для
ручной пересборки из корня репозитория:

```bash
docker build -t svarog/agent-claude:latest docker/agent-claude
```

Тег `svarog/agent-claude:latest` — то, что ждёт `executor.external.image`
(см. `agent-home/svarog.yaml`).

## «Всегда свежий» claude-code

Версия CLI намеренно не пиннится — образ ставит `@anthropic-ai/claude-code@latest`.
Docker кэширует слой `npm install`, поэтому повторная сборка может отдать
**старый** CLI. Чтобы гарантированно получить последний:

```bash
docker build --build-arg REFRESH=$(date +%s) -t svarog/agent-claude:latest docker/agent-claude
# или
docker build --no-cache -t svarog/agent-claude:latest docker/agent-claude
```

Дрейф формата стрима/флагов CLI при обновлении ловят golden-JSONL
contract-тесты адаптера (`tests/test_agent_adapters.py`), а не пиннинг версии
(ADR-0016 §8). Если после свежей сборки они краснеют — CLI сменил контракт,
адаптер надо подтянуть.
