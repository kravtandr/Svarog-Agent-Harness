"""Чистая валидация и сборка executor-настроек `svarog init` (Claude Code /
OpenCode) из уже собранных ответов.

Ничего не спрашивает и не печатает — сбор ответов (флаги CLI, интерактивные
вопросы) остаётся в `cli/main.py`; сюда попадают финальные значения.
Конфликты — `ExecutorSetupError` с готовым для пользователя текстом.
"""

from dataclasses import dataclass
from typing import Any, Literal

from svarog_harness.scaffold import (
    DEFAULT_CLAUDE_API_KEY_REF,
    DEFAULT_CLAUDE_IMAGE,
    DEFAULT_CLAUDE_OAUTH_TOKEN_REF,
    DEFAULT_OPENCODE_API_KEY_REF,
    DEFAULT_OPENCODE_IMAGE,
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
    auth: str = "subscription"
    api_key: str | None = None
    oauth_token: str | None = None


@dataclass(frozen=True)
class OpencodeAnswers:
    requested: bool
    same_as_native: bool = False
    own_creds: bool = False
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
        raise ExecutorSetupError("--executor native конфликтует с --claude-*/--opencode-* флагами")
    if not claude.requested and not opencode.requested:
        return None

    if claude.requested and claude.auth not in _VALID_CLAUDE_AUTH:
        raise ExecutorSetupError(
            f"--claude-auth: неизвестное значение {claude.auth!r} ({'|'.join(_VALID_CLAUDE_AUTH)})"
        )

    active: Literal["claude-code", "opencode"]
    if executor == "claude-code":
        if not claude.requested:
            raise ExecutorSetupError(
                "--executor claude-code указан, но настройки Claude Code не заданы"
            )
        active = "claude-code"
    elif executor == "opencode":
        if not opencode.requested:
            raise ExecutorSetupError("--executor opencode указан, но настройки OpenCode не заданы")
        active = "opencode"
    elif claude.requested and opencode.requested:
        raise ExecutorSetupError(
            "настроены и Claude Code, и OpenCode — уточните `--executor claude-code|opencode`"
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
        if opencode.same_as_native and opencode.own_creds:
            raise ExecutorSetupError(
                "--opencode-same-as-native и --opencode-own-creds взаимоисключающие"
            )
        if opencode.own_creds:
            opencode_setup = OpencodeExecutorSetup(
                model=opencode.model or native_model,
                base_url=opencode.base_url or native_base_url,
                api_key_ref=DEFAULT_OPENCODE_API_KEY_REF if opencode.api_key else None,
            )
        else:
            opencode_setup = OpencodeExecutorSetup(
                model=native_model,
                base_url=native_base_url,
                api_key_ref=native_api_key_ref,
            )

    return ExecutorSetup(active=active, claude=claude_setup, opencode=opencode_setup)


def executor_setup_yaml_patch(executor: ExecutorSetup) -> dict[str, Any]:
    """YAML-фрагмент активного external executor для существующего agent-home."""
    setup = executor.claude if executor.active == "claude-code" else executor.opencode
    assert setup is not None
    external: dict[str, Any] = {
        "adapter": executor.active,
        "image": (
            DEFAULT_CLAUDE_IMAGE if executor.active == "claude-code" else DEFAULT_OPENCODE_IMAGE
        ),
    }
    if isinstance(setup, ClaudeExecutorSetup):
        external["auth"] = setup.auth
        if setup.api_key_ref is not None:
            external["api_key_ref"] = setup.api_key_ref
        if setup.oauth_token_ref is not None:
            external["oauth_token_ref"] = setup.oauth_token_ref
    else:
        external.update(
            {
                "auth": "api-key",
                "model": setup.model,
                "base_url": setup.base_url,
            }
        )
        if setup.api_key_ref is not None:
            external["api_key_ref"] = setup.api_key_ref
    return {"executor": {"type": "external", "external": external}}
