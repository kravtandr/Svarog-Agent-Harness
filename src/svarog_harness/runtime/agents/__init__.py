"""Адаптеры внешних агентов (ADR-0016): claude-code (фаза 1); codex, opencode — фаза 4."""

from svarog_harness.config.schema import ExternalExecutorConfig
from svarog_harness.runtime.agents.claude_code import ClaudeCodeAdapter
from svarog_harness.runtime.executor import AgentAdapter


def adapter_for(cfg: ExternalExecutorConfig) -> AgentAdapter:
    """Адаптер по имени из конфига; имена валидирует Literal схемы."""
    if cfg.adapter == "claude-code":
        return ClaudeCodeAdapter()
    raise ValueError(f"неизвестный адаптер внешнего агента: {cfg.adapter}")


__all__ = ["ClaudeCodeAdapter", "adapter_for"]
