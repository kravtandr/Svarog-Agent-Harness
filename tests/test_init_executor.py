"""Тесты чистого резолвера executor-настроек `svarog init` (без CLI/I-O)."""

import pytest

from svarog_harness.cli.init_executor import (
    ClaudeAnswers,
    ExecutorSetupError,
    OpencodeAnswers,
    resolve_executor_setup,
)

_NO_CLAUDE = ClaudeAnswers(requested=False)
_NO_OPENCODE = OpencodeAnswers(requested=False)


def test_nothing_requested_returns_none() -> None:
    result = resolve_executor_setup(
        executor=None,
        claude=_NO_CLAUDE,
        opencode=_NO_OPENCODE,
        native_model="m",
        native_base_url="http://x",
        native_api_key_ref=None,
    )
    assert result is None


def test_executor_native_with_claude_requested_errors() -> None:
    with pytest.raises(ExecutorSetupError, match="native"):
        resolve_executor_setup(
            executor="native",
            claude=ClaudeAnswers(requested=True, auth="api-key", api_key="sk-x"),
            opencode=_NO_OPENCODE,
            native_model="m",
            native_base_url="http://x",
            native_api_key_ref=None,
        )


def test_unknown_executor_value_errors() -> None:
    with pytest.raises(ExecutorSetupError, match="--executor"):
        resolve_executor_setup(
            executor="bogus",
            claude=_NO_CLAUDE,
            opencode=_NO_OPENCODE,
            native_model="m",
            native_base_url="http://x",
            native_api_key_ref=None,
        )


def test_claude_api_key_with_value_sets_ref() -> None:
    result = resolve_executor_setup(
        executor=None,
        claude=ClaudeAnswers(requested=True, auth="api-key", api_key="sk-x"),
        opencode=_NO_OPENCODE,
        native_model="m",
        native_base_url="http://x",
        native_api_key_ref=None,
    )
    assert result is not None
    assert result.active == "claude-code"
    assert result.claude is not None
    assert result.claude.auth == "api-key"
    assert result.claude.api_key_ref == "CLAUDE_CODE_KEY"
    assert result.claude.oauth_token_ref is None


def test_claude_api_key_without_value_leaves_ref_none() -> None:
    result = resolve_executor_setup(
        executor=None,
        claude=ClaudeAnswers(requested=True, auth="api-key", api_key=None),
        opencode=_NO_OPENCODE,
        native_model="m",
        native_base_url="http://x",
        native_api_key_ref=None,
    )
    assert result is not None
    assert result.claude is not None
    assert result.claude.api_key_ref is None


def test_claude_subscription_always_sets_oauth_ref() -> None:
    result = resolve_executor_setup(
        executor=None,
        claude=ClaudeAnswers(requested=True, auth="subscription", oauth_token=None),
        opencode=_NO_OPENCODE,
        native_model="m",
        native_base_url="http://x",
        native_api_key_ref=None,
    )
    assert result is not None
    assert result.claude is not None
    assert result.claude.oauth_token_ref == "CLAUDE_CODE_OAUTH_TOKEN"
    assert result.claude.api_key_ref is None


def test_invalid_claude_auth_errors() -> None:
    with pytest.raises(ExecutorSetupError, match="claude-auth"):
        resolve_executor_setup(
            executor=None,
            claude=ClaudeAnswers(requested=True, auth="bogus"),
            opencode=_NO_OPENCODE,
            native_model="m",
            native_base_url="http://x",
            native_api_key_ref=None,
        )


def test_opencode_reuse_native_uses_native_values() -> None:
    result = resolve_executor_setup(
        executor=None,
        claude=_NO_CLAUDE,
        opencode=OpencodeAnswers(requested=True, same_as_native=True),
        native_model="qwen3-coder",
        native_base_url="http://localhost:8000/v1",
        native_api_key_ref="PROVIDER_API_KEY",
    )
    assert result is not None
    assert result.active == "opencode"
    assert result.opencode is not None
    assert result.opencode.model == "qwen3-coder"
    assert result.opencode.base_url == "http://localhost:8000/v1"
    assert result.opencode.api_key_ref == "PROVIDER_API_KEY"


def test_opencode_own_creds_with_values_sets_own_ref() -> None:
    result = resolve_executor_setup(
        executor=None,
        claude=_NO_CLAUDE,
        opencode=OpencodeAnswers(
            requested=True,
            own_creds=True,
            model="m2",
            base_url="http://y",
            api_key="sk-y",
        ),
        native_model="qwen3-coder",
        native_base_url="http://localhost:8000/v1",
        native_api_key_ref="PROVIDER_API_KEY",
    )
    assert result is not None
    assert result.opencode is not None
    assert result.opencode.model == "m2"
    assert result.opencode.base_url == "http://y"
    assert result.opencode.api_key_ref == "OPENCODE_API_KEY"


def test_opencode_own_creds_without_model_falls_back_to_native() -> None:
    result = resolve_executor_setup(
        executor=None,
        claude=_NO_CLAUDE,
        opencode=OpencodeAnswers(requested=True, own_creds=True, api_key=None),
        native_model="qwen3-coder",
        native_base_url="http://localhost:8000/v1",
        native_api_key_ref="PROVIDER_API_KEY",
    )
    assert result is not None
    assert result.opencode is not None
    assert result.opencode.model == "qwen3-coder"
    assert result.opencode.base_url == "http://localhost:8000/v1"
    assert result.opencode.api_key_ref is None


def test_both_requested_without_executor_errors() -> None:
    with pytest.raises(ExecutorSetupError, match=r"Claude Code.*OpenCode|OpenCode.*Claude"):
        resolve_executor_setup(
            executor=None,
            claude=ClaudeAnswers(requested=True, auth="api-key", api_key="sk-x"),
            opencode=OpencodeAnswers(requested=True, same_as_native=True),
            native_model="m",
            native_base_url="http://x",
            native_api_key_ref=None,
        )


def test_both_requested_with_explicit_executor_builds_standby() -> None:
    result = resolve_executor_setup(
        executor="opencode",
        claude=ClaudeAnswers(requested=True, auth="api-key", api_key="sk-x"),
        opencode=OpencodeAnswers(requested=True, same_as_native=True),
        native_model="m",
        native_base_url="http://x",
        native_api_key_ref="PROVIDER_API_KEY",
    )
    assert result is not None
    assert result.active == "opencode"
    assert result.claude is not None  # standby, но собран
    assert result.opencode is not None


def test_executor_claude_code_without_claude_requested_errors() -> None:
    with pytest.raises(ExecutorSetupError, match="claude-code"):
        resolve_executor_setup(
            executor="claude-code",
            claude=ClaudeAnswers(requested=False),
            opencode=OpencodeAnswers(requested=True, same_as_native=True),
            native_model="m",
            native_base_url="http://x",
            native_api_key_ref=None,
        )


def test_executor_opencode_without_opencode_requested_errors() -> None:
    with pytest.raises(ExecutorSetupError, match="opencode"):
        resolve_executor_setup(
            executor="opencode",
            claude=ClaudeAnswers(requested=True, auth="api-key", api_key="sk-x"),
            opencode=OpencodeAnswers(requested=False),
            native_model="m",
            native_base_url="http://x",
            native_api_key_ref=None,
        )


def test_opencode_conflicting_creds_flags_errors() -> None:
    with pytest.raises(ExecutorSetupError, match=r"opencode-same-as-native.*opencode-own-creds"):
        resolve_executor_setup(
            executor=None,
            claude=_NO_CLAUDE,
            opencode=OpencodeAnswers(requested=True, same_as_native=True, own_creds=True),
            native_model="m",
            native_base_url="http://x",
            native_api_key_ref=None,
        )
