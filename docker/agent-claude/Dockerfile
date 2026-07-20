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

# Superpowers — latest upstream release. Wrapper below exposes its skills
# after Svarog mounts the persistent Claude state directory.
RUN SUPERPOWERS_TAG="$(git ls-remote --refs --tags https://github.com/obra/superpowers.git \
        | awk -F/ '{print $3}' | sort -V | tail -n 1)" \
    && test -n "$SUPERPOWERS_TAG" \
    && echo "Installing Superpowers $SUPERPOWERS_TAG" \
    && git clone --depth 1 --branch "$SUPERPOWERS_TAG" \
        https://github.com/obra/superpowers.git /opt/superpowers \
    && rm -rf /opt/superpowers/.git

RUN mv /usr/local/bin/claude /usr/local/bin/claude-real \
    && printf '%s\n' \
        '#!/bin/sh' \
        'set -eu' \
        'mkdir -p "$HOME/.claude/skills"' \
        'for skill in /opt/superpowers/skills/*; do' \
        '  target="$HOME/.claude/skills/$(basename "$skill")"' \
        '  [ -e "$target" ] || ln -s "$skill" "$target"' \
        'done' \
        'exec /usr/local/bin/claude-real "$@"' \
        > /usr/local/bin/claude \
    && chmod +x /usr/local/bin/claude
