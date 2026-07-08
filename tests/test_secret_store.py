"""Тесты SecretStore и redaction (#21, ADR-0006, §12).

Все «секреты» в тестах синтетические.
"""

from pathlib import Path

import pytest

from svarog_harness.config.schema import ProviderConfig, SandboxConfig
from svarog_harness.llm.openai_compatible import ApiKeyError, resolve_api_key
from svarog_harness.sandbox.docker import DockerEnvironment
from svarog_harness.secrets import (
    EnvSecretStore,
    FileSecretStore,
    LayeredSecretStore,
    default_secret_store,
    injected_env,
    redact,
)

_FAKE = "s3cr3t-value-12345"


def test_file_store_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    store = FileSecretStore(path)
    store.set("PROVIDER_API_KEY", _FAKE)
    assert path.exists()
    assert oct(path.stat().st_mode)[-3:] == "600"  # только владелец

    reloaded = FileSecretStore(path)
    assert reloaded.get("PROVIDER_API_KEY") == _FAKE
    assert reloaded.names() == ["PROVIDER_API_KEY"]
    assert reloaded.get("NOPE") is None
    assert _FAKE in reloaded.values()


def test_env_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", _FAKE)
    store = EnvSecretStore()
    assert store.get("MY_TOKEN") == _FAKE
    assert store.get("MISSING") is None
    # env-имена не перечисляются, значения для redaction берутся по запросу.
    assert store.names() == []


def test_layered_first_hit_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file_store = FileSecretStore(tmp_path / "s.json")
    file_store.set("K", "из-файла")
    monkeypatch.setenv("K", "из-env")
    layered = LayeredSecretStore([file_store, EnvSecretStore()])
    assert layered.get("K") == "из-файла"  # файл раньше env

    layered2 = LayeredSecretStore([EnvSecretStore(), file_store])
    assert layered2.get("K") == "из-env"


def test_default_store_env_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONLY_ENV", _FAKE)
    store = default_secret_store(tmp_path / "missing.json")
    assert store.get("ONLY_ENV") == _FAKE


# --- resolve_api_key через store ---


def test_resolve_api_key_from_store(tmp_path: Path) -> None:
    file_store = FileSecretStore(tmp_path / "s.json")
    file_store.set("PROVIDER_API_KEY", _FAKE)
    cfg = ProviderConfig(base_url="http://x/v1", model="m", api_key_ref="PROVIDER_API_KEY")
    assert resolve_api_key(cfg, file_store) == _FAKE


def test_resolve_api_key_missing_raises(tmp_path: Path) -> None:
    cfg = ProviderConfig(base_url="http://x/v1", model="m", api_key_ref="ABSENT_KEY")
    with pytest.raises(ApiKeyError, match="ABSENT_KEY"):
        resolve_api_key(cfg, FileSecretStore(tmp_path / "empty.json"))


def test_resolve_api_key_none_is_stub() -> None:
    cfg = ProviderConfig(base_url="http://localhost:8000/v1", model="local")
    assert resolve_api_key(cfg) == "not-needed"


# --- redaction ---


def test_redact_replaces_values() -> None:
    text = f"токен: {_FAKE} конец"
    result = redact(text, frozenset({_FAKE}))
    assert _FAKE not in result
    assert "[REDACTED]" in result


def test_redact_longest_first() -> None:
    text = "abcdef"
    result = redact(text, frozenset({"abc", "abcdef"}))
    # Более длинное значение вырезается целиком, а не оставляет хвост "def".
    assert result == "[REDACTED]"


def test_redact_empty_noop() -> None:
    assert redact("чистый текст", frozenset()) == "чистый текст"


# --- инжекция в окружение sandbox ---


def test_injected_env_only_requested(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "s.json")
    store.set("GH_TOKEN", _FAKE)
    store.set("OTHER", "не-нужен")
    env = injected_env(store, ["GH_TOKEN"])
    assert env == {"GH_TOKEN": _FAKE}


def test_docker_run_args_include_injected_env(tmp_path: Path) -> None:
    env = DockerEnvironment(tmp_path, SandboxConfig(), env={"GH_TOKEN": _FAKE})
    args = env.run_args()
    assert f"GH_TOKEN={_FAKE}" in args


async def test_loop_redacts_secret_in_tool_output(tmp_path: Path) -> None:
    """Значение секрета из вывода команды вырезается до контекста и trace (§12)."""
    from collections.abc import AsyncIterator, Callable

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from svarog_harness.config.schema import AutonomyMode, PoliciesConfig, RuntimeConfig
    from svarog_harness.llm.provider import (
        ChatMessage,
        CompletionResult,
        ModelProvider,
        ToolCallRequest,
        ToolDefinition,
        Usage,
    )
    from svarog_harness.policy.engine import PolicyEngine
    from svarog_harness.runtime.loop import AgentLoop
    from svarog_harness.sandbox.local import LocalEnvironment
    from svarog_harness.storage.db import create_engine, create_session_factory, init_db
    from svarog_harness.storage.models import Message
    from svarog_harness.tools.registry import ToolRegistry
    from svarog_harness.tools.shell import BashTool
    from svarog_harness.trace.recorder import TraceRecorder

    class ScriptedProvider(ModelProvider):
        def __init__(self, turns: list[CompletionResult]) -> None:
            self.turns = list(turns)
            self.seen: list[list[ChatMessage]] = []

        async def complete(
            self,
            messages: list[ChatMessage],
            tools: list[ToolDefinition],
            *,
            on_text_delta: "Callable[[str], None] | None" = None,
        ) -> CompletionResult:
            self.seen.append(list(messages))
            return self.turns.pop(0)

    db_path = tmp_path / "db" / "s.sqlite3"
    init_db(db_path)
    engine = create_engine(db_path)
    factory = create_session_factory(engine)

    async def _run(session: AsyncSession) -> ScriptedProvider:
        registry = ToolRegistry()
        registry.register(BashTool(LocalEnvironment(tmp_path)))
        provider = ScriptedProvider(
            [
                CompletionResult(
                    content="",
                    tool_calls=(
                        ToolCallRequest(
                            id="c1", name="bash", arguments_json=f'{{"command": "echo {_FAKE}"}}'
                        ),
                    ),
                    usage=Usage(10, 5),
                ),
                CompletionResult(content="готово", usage=Usage(10, 5)),
            ]
        )
        loop = AgentLoop(
            provider,
            registry,
            TraceRecorder(session),
            RuntimeConfig(),
            PolicyEngine(autonomy=AutonomyMode.YOLO, policies=PoliciesConfig(), workspace=tmp_path),
            tmp_path,
            model_name="test",
            secret_values=frozenset({_FAKE}),
        )
        await loop.run("напечатай секрет", AutonomyMode.YOLO)
        return provider

    async def _check(session: AsyncSession) -> None:
        rows: AsyncIterator[Message] = (await session.execute(select(Message))).scalars()
        assert all(_FAKE not in (m.content.get("content") or "") for m in rows)

    async with factory() as session:
        provider = await _run(session)
    async with factory() as session:
        await _check(session)
    await engine.dispose()

    # Модель тоже не увидела значение секрета в tool-результате.
    tool_msg = provider.seen[-1][-1]
    assert _FAKE not in tool_msg.content
    assert "[REDACTED]" in tool_msg.content
