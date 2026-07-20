"""Чистая валидация и сборка executor-настроек `svarog init` (Claude Code /
OpenCode) из уже собранных ответов.

Ничего не спрашивает и не печатает — сбор ответов (флаги CLI, интерактивные
вопросы) остаётся в `cli/main.py`; сюда попадают финальные значения.
Конфликты — `ExecutorSetupError` с готовым для пользователя текстом.
"""

from dataclasses import dataclass
from typing import Literal

from svarog_harness.scaffold import (
    DEFAULT_CLAUDE_API_KEY_REF,
    DEFAULT_CLAUDE_OAUTH_TOKEN_REF,
    DEFAULT_OPENCODE_API_KEY_REF,
    ClaudeExecutorSetup,
    ExecutorSetup,
    OpencodeExecutorSetup,
)

_VALID_EXECUTORS = ("native", "claude-code", "opencode")
_VALID_CLAUDE_AUTH = ("api-key", "subscription")


class ExecutorSetupError(ValueError):
    """Невалидная или противоречивая комбинация флагов/ответов `init`."""


@dataclass(frozen=True)
class ClaudeAnswers:
    requested: bool
    auth: str = "api-key"
    api_key: str | None = None
    oauth_token: str | None = None


@dataclass(frozen=True)
class OpencodeAnswers:
    requested: bool
    reuse_native: bool = True
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


def resolve_executor_setup(
    *,
    executor: str | None,
    claude: ClaudeAnswers,
    opencode: OpencodeAnswers,
    native_model: str,
    native_base_url: str,
    native_api_key_ref: str | None,
) -> ExecutorSetup | None:
    if executor is not None and executor not in _VALID_EXECUTORS:
        raise ExecutorSetupError(
            f"--executor: неизвестное значение {executor!r} ({'|'.join(_VALID_EXECUTORS)})"
        )
    if executor == "native" and (claude.requested or opencode.requested):
        raise ExecutorSetupError(
            "--executor native конфликтует с --claude-*/--opencode-* флагами"
        )
    if not claude.requested and not opencode.requested:
        return None

    if claude.requested and claude.auth not in _VALID_CLAUDE_AUTH:
        raise ExecutorSetupError(
            f"--claude-auth: неизвестное значение {claude.auth!r} "
            f"({'|'.join(_VALID_CLAUDE_AUTH)})"
        )

    active: Literal["claude-code", "opencode"]
    if executor == "claude-code":
        active = "claude-code"
    elif executor == "opencode":
        active = "opencode"
    elif claude.requested and opencode.requested:
        raise ExecutorSetupError(
            "настроены и Claude Code, и OpenCode — уточните "
            "`--executor claude-code|opencode`"
        )
    elif claude.requested:
        active = "claude-code"
    else:
        active = "opencode"

    claude_setup: ClaudeExecutorSetup | None = None
    if claude.requested:
        if claude.auth == "subscription":
            claude_setup = ClaudeExecutorSetup(
                auth="subscription",
                api_key_ref=None,
                oauth_token_ref=DEFAULT_CLAUDE_OAUTH_TOKEN_REF,
            )
        else:
            claude_setup = ClaudeExecutorSetup(
                auth="api-key",
                api_key_ref=DEFAULT_CLAUDE_API_KEY_REF if claude.api_key else None,
                oauth_token_ref=None,
            )

    opencode_setup: OpencodeExecutorSetup | None = None
    if opencode.requested:
        if opencode.reuse_native:
            opencode_setup = OpencodeExecutorSetup(
                model=native_model,
                base_url=native_base_url,
                api_key_ref=native_api_key_ref,
            )
        else:
            opencode_setup = OpencodeExecutorSetup(
                model=opencode.model or native_model,
                base_url=opencode.base_url or native_base_url,
                api_key_ref=DEFAULT_OPENCODE_API_KEY_REF if opencode.api_key else None,
            )

    return ExecutorSetup(active=active, claude=claude_setup, opencode=opencode_setup)
