"""Тесты ModelProvider: сборка streaming-ответа, tool calls, токены, стоимость."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from openai import AsyncOpenAI

from svarog_harness.config.schema import ProviderConfig
from svarog_harness.llm.openai_compatible import (
    ApiKeyError,
    OpenAICompatibleProvider,
    resolve_api_key,
)
from svarog_harness.llm.provider import ChatMessage, ToolCallRequest, ToolDefinition


def _chunk(
    *,
    content: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str | None = None,
    usage: Any | None = None,
    choices: bool = True,
) -> Any:
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice] if choices else [], usage=usage)


def _tc_delta(index: int, *, call_id: str = "", name: str = "", arguments: str = "") -> Any:
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=call_id, function=function)


class _FakeStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self) -> Any:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _FakeClient:
    """Дублирует client.chat.completions.create и запоминает kwargs вызова."""

    def __init__(self, chunks: list[Any]) -> None:
        self.kwargs: dict[str, Any] = {}

        async def create(**kwargs: Any) -> _FakeStream:
            self.kwargs = kwargs
            return _FakeStream(chunks)

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def _provider(
    chunks: list[Any], **cfg_overrides: Any
) -> tuple[OpenAICompatibleProvider, _FakeClient]:
    cfg = ProviderConfig(base_url="http://localhost:8000/v1", model="test-model", **cfg_overrides)
    client = _FakeClient(chunks)
    return OpenAICompatibleProvider(cfg, client=cast(AsyncOpenAI, client)), client


def _to_namespace(value: Any) -> Any:
    """Рекурсивно превратить dict в SimpleNamespace — для вложенных полей usage
    вроде prompt_tokens_details (getattr должен работать, как на реальном SDK-объекте)."""
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in value.items()})
    return value


def _usage_with_extra(*, prompt_tokens: int, completion_tokens: int, extra: dict[str, Any]) -> Any:
    """usage-объект с произвольными доп. полями — диалекты cached-токенов провайдеров."""
    fields = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    fields.update({k: _to_namespace(v) for k, v in extra.items()})
    return SimpleNamespace(**fields)


def _provider_with_usage(
    *, prompt_tokens: int, completion_tokens: int, extra: dict[str, Any]
) -> OpenAICompatibleProvider:
    """Провайдер, чей единственный ответ несёт usage с диалект-специфичными полями."""
    usage = _usage_with_extra(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, extra=extra
    )
    provider, _ = _provider(
        [_chunk(content="ok", finish_reason="stop"), _chunk(usage=usage, choices=False)]
    )
    return provider


async def test_streams_text_and_reports_usage() -> None:
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20)
    provider, client = _provider(
        [
            _chunk(content="Hel"),
            _chunk(content="lo", finish_reason="stop"),
            _chunk(usage=usage, choices=False),
        ]
    )
    deltas: list[str] = []
    result = await provider.complete(
        [ChatMessage(role="user", content="hi")], [], on_text_delta=deltas.append
    )
    assert result.content == "Hello"
    assert deltas == ["Hel", "lo"]
    assert result.usage.prompt_tokens == 100
    assert result.usage.total_tokens == 120
    assert result.finish_reason == "stop"
    assert client.kwargs["stream"] is True
    assert client.kwargs["model"] == "test-model"


async def test_assembles_tool_calls_from_deltas() -> None:
    provider, client = _provider(
        [
            _chunk(tool_calls=[_tc_delta(0, call_id="call_1", name="read_file", arguments='{"pa')]),
            _chunk(tool_calls=[_tc_delta(0, arguments='th": "a.txt"}')]),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    tool = ToolDefinition(name="read_file", description="d", input_schema={"type": "object"})
    result = await provider.complete([ChatMessage(role="user", content="go")], [tool])
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "read_file"
    assert call.parse_arguments() == {"path": "a.txt"}
    assert client.kwargs["tools"][0]["function"]["name"] == "read_file"


async def test_recovers_tool_call_leaked_into_content() -> None:
    # Harmony-leak: сервер отдал вызов инструмента текстом, tool_calls пуст.
    leaked = (
        "analysis...assistantcommentary to=functions.remember json"
        '{"file": "user/profile.md", "operation": "append", "content": "x"}'
        "assistantfinalЗапомнил."
    )
    provider, _ = _provider(
        [_chunk(content=leaked[:40]), _chunk(content=leaked[40:], finish_reason="stop")]
    )
    result = await provider.complete([ChatMessage(role="user", content="запомни")], [])
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "remember"
    assert result.tool_calls[0].parse_arguments()["operation"] == "append"
    # Протёкшие каналы — не ответ пользователю.
    assert result.content == ""
    assert result.leak_suspected is False


async def test_flags_unparseable_leak_as_suspected() -> None:
    leaked = "commentary to=functions.remember json{'file': 'user/profile.md'}final Запомнил."
    provider, _ = _provider([_chunk(content=leaked, finish_reason="stop")])
    result = await provider.complete([ChatMessage(role="user", content="запомни")], [])
    assert result.tool_calls == ()
    assert result.leak_suspected is True
    assert result.content == leaked


async def test_structured_tool_calls_skip_leak_fallback() -> None:
    provider, _ = _provider(
        [
            _chunk(
                content="to=functions.remember упоминание в тексте",
                tool_calls=[_tc_delta(0, call_id="c1", name="remember", arguments="{}")],
            ),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    result = await provider.complete([ChatMessage(role="user", content="go")], [])
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "c1"
    assert result.leak_suspected is False


async def test_estimates_tokens_when_usage_missing() -> None:
    provider, _ = _provider([_chunk(content="x" * 40, finish_reason="stop")])
    result = await provider.complete([ChatMessage(role="user", content="y" * 400)], [])
    assert result.usage.completion_tokens == 10
    assert result.usage.prompt_tokens == 100


async def test_computes_cost_from_configured_prices() -> None:
    usage = SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=500_000)
    provider, _ = _provider(
        [_chunk(content="ok", finish_reason="stop"), _chunk(usage=usage, choices=False)],
        input_usd_per_mtok=3.0,
        output_usd_per_mtok=15.0,
    )
    result = await provider.complete([ChatMessage(role="user", content="hi")], [])
    assert result.cost_usd == pytest.approx(3.0 + 7.5)


async def test_serializes_assistant_tool_history() -> None:
    provider, client = _provider([_chunk(content="done", finish_reason="stop")])
    call = ToolCallRequest(id="call_1", name="bash", arguments_json='{"command": "ls"}')
    messages = [
        ChatMessage(role="assistant", content="", tool_calls=(call,)),
        ChatMessage(role="tool", content="file.txt", tool_call_id="call_1"),
    ]
    await provider.complete(messages, [])
    sent = client.kwargs["messages"]
    assert sent[0]["tool_calls"][0]["function"]["name"] == "bash"
    assert sent[1]["tool_call_id"] == "call_1"


def test_parse_arguments_rejects_invalid_json() -> None:
    call = ToolCallRequest(id="1", name="bash", arguments_json="{broken")
    with pytest.raises(ValueError, match="валидным JSON"):
        call.parse_arguments()


def test_parse_arguments_rejects_non_object() -> None:
    call = ToolCallRequest(id="1", name="bash", arguments_json='["list"]')
    with pytest.raises(ValueError, match="JSON-объектом"):
        call.parse_arguments()


def test_resolve_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ProviderConfig(base_url="u", model="m", api_key_ref="TEST_SVAROG_KEY")
    monkeypatch.setenv("TEST_SVAROG_KEY", "sk-value")
    assert resolve_api_key(cfg) == "sk-value"


def test_resolve_api_key_missing_ref_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ProviderConfig(base_url="u", model="m", api_key_ref="TEST_SVAROG_KEY")
    monkeypatch.delenv("TEST_SVAROG_KEY", raising=False)
    with pytest.raises(ApiKeyError, match="TEST_SVAROG_KEY"):
        resolve_api_key(cfg)


def test_resolve_api_key_defaults_to_stub() -> None:
    cfg = ProviderConfig(base_url="u", model="m")
    assert resolve_api_key(cfg) == "not-needed"


async def test_legacy_function_call_is_not_lost() -> None:
    # Старые серверы шлют вызов через deprecated-поле delta.function_call.
    fc1 = SimpleNamespace(name="read_file", arguments='{"pa')
    fc2 = SimpleNamespace(name=None, arguments='th": "a.txt"}')
    provider, _ = _provider(
        [
            _chunk_with_function_call(fc1),
            _chunk_with_function_call(fc2, finish_reason="function_call"),
        ]
    )
    result = await provider.complete([ChatMessage(role="user", content="go")], [])
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.name == "read_file"
    assert call.parse_arguments() == {"path": "a.txt"}
    # id сервер не прислал — провайдер генерирует уникальный сам.
    assert call.id


def _chunk_with_function_call(fc: Any, *, finish_reason: str | None = None) -> Any:
    chunk = _chunk(finish_reason=finish_reason)
    chunk.choices[0].delta.function_call = fc
    return chunk


async def test_generates_id_when_server_omits_it() -> None:
    provider, _ = _provider(
        [
            _chunk(tool_calls=[_tc_delta(0, name="read_file", arguments='{"path": "a.txt"}')]),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    result = await provider.complete([ChatMessage(role="user", content="go")], [])
    assert result.tool_calls[0].id.startswith("call-")


# --- Блок A §3: cached_tokens — три диалекта провайдеров ---------------------


async def test_usage_reads_cached_tokens_from_prompt_tokens_details() -> None:
    """OpenAI/Qwen/Mistral: usage.prompt_tokens_details.cached_tokens."""
    provider = _provider_with_usage(
        prompt_tokens=100,
        completion_tokens=10,
        extra={"prompt_tokens_details": {"cached_tokens": 64}},
    )
    result = await provider.complete([ChatMessage(role="user", content="привет")], [])
    assert result.usage.cached_tokens == 64


async def test_usage_reads_top_level_cached_tokens() -> None:
    """StepFun/Moonshot: верхнеуровневый usage.cached_tokens."""
    provider = _provider_with_usage(
        prompt_tokens=100, completion_tokens=10, extra={"cached_tokens": 32}
    )
    result = await provider.complete([ChatMessage(role="user", content="привет")], [])
    assert result.usage.cached_tokens == 32


async def test_usage_reads_prompt_cache_hit_tokens() -> None:
    """DeepSeek/SiliconFlow: usage.prompt_cache_hit_tokens."""
    provider = _provider_with_usage(
        prompt_tokens=100, completion_tokens=10, extra={"prompt_cache_hit_tokens": 16}
    )
    result = await provider.complete([ChatMessage(role="user", content="привет")], [])
    assert result.usage.cached_tokens == 16


async def test_usage_without_cache_fields_is_zero() -> None:
    provider = _provider_with_usage(prompt_tokens=100, completion_tokens=10, extra={})
    result = await provider.complete([ChatMessage(role="user", content="привет")], [])
    assert result.usage.cached_tokens == 0
