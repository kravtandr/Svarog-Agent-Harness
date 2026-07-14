"""Адаптеры внешних агентов (ADR-0016, фазы 1 и 4).

Матрица capabilities (§1): у claude-code — hooks + resume + mcp (полный
tier 2); у codex/opencode — resume без hooks/mcp, поэтому supervised с ними
отклоняется fail-closed (§6), а память/скиллы не пробрасываются.
"""

from svarog_harness.config.schema import ExternalExecutorConfig
from svarog_harness.runtime.agents.claude_code import ClaudeCodeAdapter
from svarog_harness.runtime.agents.codex import CodexAdapter
from svarog_harness.runtime.agents.opencode import OpencodeAdapter
from svarog_harness.runtime.executor import AgentAdapter


def adapter_for(cfg: ExternalExecutorConfig) -> AgentAdapter:
    """Адаптер по имени из конфига; имена валидирует Literal схемы."""
    match cfg.adapter:
        case "claude-code":
            return ClaudeCodeAdapter()
        case "codex":
            return CodexAdapter()
        case "opencode":
            return OpencodeAdapter()
    raise ValueError(f"неизвестный адаптер внешнего агента: {cfg.adapter}")


__all__ = ["ClaudeCodeAdapter", "CodexAdapter", "OpencodeAdapter", "adapter_for"]
