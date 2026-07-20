# Sandbox-образ агента OpenCode (ADR-0016)

Образ data-plane для `executor: external` с `adapter: opencode`. Внутри —
Node (сам агент `opencode`), git (Flow C), ripgrep (инструменты glob/grep:
без `rg` в PATH OpenCode пытается скачать бинарь с github.com, что в
закрытой internal-сети sandbox'а молча падает — glob/grep перестают
работать). Хуков нет — cooperative-tier (supervised, память/скиллы через
MCP) с этим адаптером недоступен, только containment
(`enforcement: containment`).

## Сборка

`svarog init` собирает образ автоматически, когда настроен OpenCode. Для
ручной пересборки из корня репозитория:

```bash
docker build -t svarog/agent-opencode:latest docker/agent-opencode
```

Тег `svarog/agent-opencode:latest` — то, что ждёт `executor.external.image`
(см. `agent-home/svarog.yaml`).

## «Всегда свежий» opencode

Версия CLI намеренно не пинится — образ ставит `opencode-ai@latest`. Docker
кэширует слой `npm install`, поэтому повторная сборка может отдать
**старый** CLI. Чтобы гарантированно получить последний:

```bash
docker build --build-arg REFRESH=$(date +%s) -t svarog/agent-opencode:latest docker/agent-opencode
# или
docker build --no-cache -t svarog/agent-opencode:latest docker/agent-opencode
```

Дрейф формата стрима/флагов CLI при обновлении ловят golden-JSONL
contract-тесты адаптера (`tests/test_agent_adapters.py`), а не пиннинг версии
(ADR-0016 §8). Если после свежей сборки они краснеют — CLI сменил контракт,
адаптер надо подтянуть.

## Провайдер и модель

Адаптер (`OpencodeAdapter.base_url_env`) направляет OpenCode на bridge-прокси
Svarog через `OPENAI_BASE_URL`/`OPENAI_API_KEY` — OpenCode видит это как
встроенный провайдер `openai`. Это работает «из коробки», только если
`executor.external.base_url` (ADR-0016 §3) — эндпоинт, отвечающий именами
моделей OpenAI (`gpt-4o`, `o3`, …) и по умолчанию выбираемый OpenCode.

Надёжный способ — задать модель явно:

```yaml
executor:
  external:
    adapter: opencode
    model: openai/gpt-oss-120b   # имя модели у вашего upstream'а
```

С `model` Svarog при каждом запуске пишет в state volume агента managed-конфиг
`.config/opencode/opencode.jsonc` с custom-провайдером на
`@ai-sdk/openai-compatible` (chat-completions поверх bridge). Это ещё и
обходит Responses API, который встроенный провайдер `openai` использует по
умолчанию: у произвольных OpenAI-совместимых upstream'ов (OpenRouter, LiteLLM,
vLLM) resume сессии через Responses API падает с «Invalid Responses API
request». Без `model` конфиг не генерируется — OpenCode выбирает провайдера
и модель сам. Только `auth: api-key` — subscription не поддержан адаптером.
