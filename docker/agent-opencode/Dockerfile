# Sandbox-образ внешнего агента OpenCode (ADR-0016 фаза 4).
#
# node — сам агент (opencode); git — Flow C (агент коммитит сам внутри
# workspace); ca-certificates — TLS к bridge-прокси; ripgrep — инструменты
# glob/grep OpenCode: без `rg` в PATH он пытается скачать бинарь с github.com,
# что в internal-сети sandbox'а (единственный hop — bridge, ADR-0016 §2)
# молча падает, и все glob/grep-вызовы мертвы. python3 не нужен —
# у OpenCode нет hook-моста (capabilities().hooks=False, tier 2 недоступен).
#
# Версия opencode-ai НЕ пинится намеренно — по аналогии с agent-claude.
# Дрейф stream-формата ловят golden-JSONL contract-тесты адаптера
# (tests/test_agent_adapters.py) — не пиннинг (ADR-0016 §8).
FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Cache-bust: чтобы «всегда свежий» работал вопреки кэшу слоёв, передайте
# меняющийся REFRESH — `--build-arg REFRESH=$(date +%s)` пересобирает слой
# ниже и тянет последний opencode-ai (см. README).
ARG REFRESH=unset
RUN npm install -g opencode-ai@latest

# Бинарь на месте — иначе сборка падает здесь, а не в первом run.
RUN opencode --version

# OpenCode loads Superpowers from a local plugin installed in this image.
# Resolve the latest upstream release during image build; sandbox runs stay offline.
RUN SUPERPOWERS_TAG="$(git ls-remote --refs --tags https://github.com/obra/superpowers.git \
        | awk -F/ '{print $3}' | sort -V | tail -n 1)" \
    && test -n "$SUPERPOWERS_TAG" \
    && echo "Installing Superpowers $SUPERPOWERS_TAG" \
    && npm install --omit=dev --prefix /opt/superpowers \
        "superpowers@git+https://github.com/obra/superpowers.git#$SUPERPOWERS_TAG"
