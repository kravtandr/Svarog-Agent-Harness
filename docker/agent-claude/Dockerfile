# Sandbox-образ внешнего агента Claude Code (ADR-0016).
#
# node — сам агент (claude); python3 — hook-скрипт tier 2 и MCP-пробы;
# git — Flow C (агент коммитит сам внутри workspace); ca-certificates — TLS
# к bridge-прокси.
#
# Версия claude-code НЕ пинится намеренно: образ всегда тянет свежий CLI.
# Дрейф stream-формата/флагов ловят golden-JSONL contract-тесты адаптера
# (tests/test_agent_adapters.py) и doctor-gate — не пиннинг (ADR-0016 §8).
FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Cache-bust: чтобы «всегда свежий» работал вопреки кэшу слоёв, передайте
# меняющийся REFRESH — `--build-arg REFRESH=$(date +%s)` пересобирает слой
# ниже и тянет последний claude-code (см. README).
ARG REFRESH=unset
RUN npm install -g @anthropic-ai/claude-code@latest

# Бинарь на месте — иначе сборка падает здесь, а не в первом run.
RUN claude --version
