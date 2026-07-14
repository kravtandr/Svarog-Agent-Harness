"""Снимок безопасностно-значимого конфига на старте run (ADR-0015 §0.4).

Autonomy и tenant-role уже заморожены в run (ADR-0010/0013). Но провайдер,
MCP-серверы и policy-правила подтягивались из mutable-yaml на каждом resume —
project-config способен объявить host-side stdio-MCP или ослабить правила между
стартом и resume (`config/loader.py`, `orchestrator.resume`).

Здесь считается детерминированный хеш effective-конфига этих полей. Он
сохраняется в записи run на старте; resume сверяет текущий конфиг со снимком.
Расхождение — сигнал, что control-plane run'а изменился под ним: headless
fail-closed (resume отклоняется), а не тихое исполнение под новым конфигом.
"""

import hashlib
import json
from pathlib import Path

from svarog_harness.config.schema import SvarogConfig
from svarog_harness.policy.rules import load_policy_rules

# Ключ снимка в Run.meta.
CONFIG_HASH_META_KEY = "config_hash"


def effective_config_snapshot(cfg: SvarogConfig, workspace: Path) -> dict[str, object]:
    """Сериализуемый срез полей, которые НЕ должны меняться под работающим run.

    Провайдер (endpoint/модель/ref ключа), MCP-серверы (команда исполнения на
    хосте), policy-правила (project + профили + protected-ветки) и secrets-refs.
    Значения секретов не входят — только их имена (ADR-0006).
    """
    providers = {
        name: {
            "type": p.type,
            "base_url": p.base_url,
            "model": p.model,
            "api_key_ref": p.api_key_ref,
        }
        for name, p in sorted(cfg.models.providers.items())
    }
    mcp = {
        name: {
            "command": s.command,
            "args": list(s.args),
            "env_refs": sorted(s.env_refs),
            "risk": s.risk,
        }
        for name, s in sorted(cfg.mcp.servers.items())
    }
    rules = [
        {"match": r.match, "decision": r.decision, "paths": sorted(r.paths)}
        for r in load_policy_rules(workspace)
    ]
    profiles = {
        name: {
            "require_approval": sorted(prof.require_approval),
            "notify": sorted(prof.notify),
        }
        for name, prof in sorted(cfg.policies.profiles.items())
    }
    return {
        "default_provider": cfg.models.default,
        "providers": providers,
        "mcp": mcp,
        "policy": {
            "protected_branches": sorted(cfg.policies.protected_branches),
            "profiles": profiles,
            "rules": rules,
        },
        "secrets_refs": sorted(cfg.secrets.inject),
        # Data-plane run'а (ADR-0016): подмена native↔external или адаптера/образа
        # между стартом и resume — изменение исполняющей стороны, fail-closed.
        "executor": {
            "type": cfg.executor.type,
            "external": None
            if cfg.executor.external is None
            else {
                "adapter": cfg.executor.external.adapter,
                "image": cfg.executor.external.image,
                "auth": cfg.executor.external.auth,
                "api_key_ref": cfg.executor.external.api_key_ref,
            },
        },
    }


def config_digest(cfg: SvarogConfig, workspace: Path) -> str:
    """SHA-256 канонической JSON-сериализации снимка (стабилен между процессами)."""
    snapshot = effective_config_snapshot(cfg, workspace)
    canonical = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
