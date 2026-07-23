"""Адаптеры внешних агентов (ADR-0016, фазы 1 и 4).

Матрица capabilities (§1) — источник истины в самих адаптерах:

* claude-code — hooks + resume + mcp (полный tier 2);
* opencode — resume + mcp, без hooks (mcp включён спайком 21.07.2026:
  мост подключается remote-секцией managed-конфига);
* codex — только resume: его MCP-конфиг (TOML, stdio) несовместим с
  HTTP-bridge.

Без hooks supervised отклоняется fail-closed (§6). Без mcp не пробрасываются
память, скиллы и документация Svarog (`read_svarog_docs`) — то есть у codex.
"""

from svarog_harness.config.schema import ExternalExecutorConfig
from svarog_harness.runtime.agents.claude_code import ClaudeCodeAdapter
from svarog_harness.runtime.agents.codex import CodexAdapter
from svarog_harness.runtime.agents.opencode import OpencodeAdapter
from svarog_harness.runtime.executor import AgentAdapter

# Запас клиентских таймаутов агента (hook, MCP-вызов) поверх approval_grace_sec:
# гейт должен успеть отработать grace + suspend ДО того, как клиент бросит
# вызов, иначе run завершается completed вместо waiting_approval (§7).
CLIENT_GATE_TIMEOUT_MARGIN_SEC = 60


def adapter_for(cfg: ExternalExecutorConfig) -> AgentAdapter:
    """Адаптер по имени из конфига; имена валидирует Literal схемы."""
    match cfg.adapter:
        case "claude-code":
            return ClaudeCodeAdapter(
                hook_timeout_sec=cfg.approval_grace_sec + CLIENT_GATE_TIMEOUT_MARGIN_SEC
            )
        case "codex":
            return CodexAdapter()
        case "opencode":
            return OpencodeAdapter()
    raise ValueError(f"неизвестный адаптер внешнего агента: {cfg.adapter}")


__all__ = [
    "CLIENT_GATE_TIMEOUT_MARGIN_SEC",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "OpencodeAdapter",
    "adapter_for",
]
