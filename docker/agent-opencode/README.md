# Sandbox-образ агента OpenCode (ADR-0016)

Образ data-plane для `executor: external` с `adapter: opencode`. Внутри —
Node (сам агент `opencode`), git (Flow C). Хуков нет — cooperative-tier
(supervised, память/скиллы через MCP) с этим адаптером недоступен,
только containment (`enforcement: containment`).

## Сборка

```bash
# из корня репозитория
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

Если ваш апстрим (LiteLLM/vLLM/OpenRouter/собственный роутер) отдаёт модели
под другими именами — до запуска нужно подложить в state volume агента
(`agent-state/opencode/`) свой `.config/opencode/opencode.json` с
custom-provider секцией (`@ai-sdk/openai-compatible`, см. docs.opencode.ai/
providers) и явно указанной моделью по умолчанию; сейчас Svarog это не
генерирует автоматически. Только `auth: api-key` — subscription не
поддержан адаптером.
