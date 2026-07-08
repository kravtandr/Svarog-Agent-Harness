"""Openai-compatible реализация ModelProvider (единственная в MVP, ADR-0001).

Работает с любым сервером, говорящим на OpenAI chat completions API:
vLLM, llama.cpp, LiteLLM, OpenRouter, сам OpenAI. Всегда использует
streaming; retries и timeouts делегированы openai SDK.
"""

from collections.abc import Callable
from typing import Any

from openai import AsyncOpenAI

from svarog_harness.config.schema import ModelsConfig, ProviderConfig
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.secrets import EnvSecretStore, SecretStore


class ApiKeyError(Exception):
    """api_key_ref задан, но секрет не найден."""


def resolve_api_key(cfg: ProviderConfig, store: SecretStore | None = None) -> str:
    """Разрешить api_key_ref в значение ключа через SecretStore (ADR-0006).

    Агент видит только имя (api_key_ref); значение берётся из store (файл или
    env) на execution-слое. Без ссылки возвращается заглушка: локальные
    серверы (vLLM, llama.cpp) ключ не проверяют, а SDK требует непустое значение.
    """
    if cfg.api_key_ref is None:
        return "not-needed"
    resolver = store if store is not None else EnvSecretStore()
    value = resolver.get(cfg.api_key_ref)
    if not value:
        raise ApiKeyError(
            f"секрет '{cfg.api_key_ref}' не найден в SecretStore/окружении; "
            f"добавьте его в secrets-файл, экспортируйте env-переменную "
            f"или уберите api_key_ref для локальной модели"
        )
    return value


def default_provider(
    models_cfg: ModelsConfig, store: SecretStore | None = None
) -> "OpenAICompatibleProvider":
    """Провайдер для default-модели из конфигурации (валидность ссылки проверена схемой)."""
    return OpenAICompatibleProvider(models_cfg.providers[models_cfg.default], store=store)


def _to_openai_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for msg in messages:
        item: dict[str, Any] = {"role": msg.role, "content": msg.content}
        if msg.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments_json},
                }
                for call in msg.tool_calls
            ]
        if msg.tool_call_id is not None:
            item["tool_call_id"] = msg.tool_call_id
        result.append(item)
    return result


def _to_openai_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]


def _estimate_tokens(text: str) -> int:
    """Грубая оценка на случай, если сервер не вернул usage (~4 символа/токен)."""
    return max(1, len(text) // 4)


class _ToolCallAccumulator:
    """Сборка tool call из streaming-дельт: id/name приходят один раз, аргументы — кусками."""

    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments = ""

    def to_request(self) -> ToolCallRequest:
        return ToolCallRequest(id=self.id, name=self.name, arguments_json=self.arguments)


class OpenAICompatibleProvider(ModelProvider):
    def __init__(
        self,
        cfg: ProviderConfig,
        *,
        client: AsyncOpenAI | None = None,
        store: SecretStore | None = None,
    ) -> None:
        self._cfg = cfg
        # Инжекция клиента — для тестов; в бою собираем сами.
        self._client = client or AsyncOpenAI(
            base_url=cfg.base_url,
            api_key=resolve_api_key(cfg, store),
            timeout=cfg.timeout_sec,
            max_retries=cfg.max_retries,
        )

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        kwargs: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": _to_openai_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = _to_openai_tools(tools)

        stream = await self._client.chat.completions.create(**kwargs)

        content_parts: list[str] = []
        calls: dict[int, _ToolCallAccumulator] = {}
        usage: Usage | None = None
        finish_reason: str | None = None

        async for chunk in stream:
            if chunk.usage is not None:
                usage = Usage(
                    prompt_tokens=chunk.usage.prompt_tokens,
                    completion_tokens=chunk.usage.completion_tokens,
                )
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason is not None:
                finish_reason = choice.finish_reason
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                content_parts.append(delta.content)
                if on_text_delta is not None:
                    on_text_delta(delta.content)
            for tc in delta.tool_calls or []:
                acc = calls.setdefault(tc.index, _ToolCallAccumulator())
                if tc.id:
                    acc.id = tc.id
                if tc.function is not None:
                    if tc.function.name:
                        acc.name = tc.function.name
                    if tc.function.arguments:
                        acc.arguments += tc.function.arguments

        content = "".join(content_parts)
        if usage is None:
            prompt_text = "".join(m.content for m in messages)
            completion_text = content + "".join(a.arguments for a in calls.values())
            usage = Usage(
                prompt_tokens=_estimate_tokens(prompt_text),
                completion_tokens=_estimate_tokens(completion_text),
            )
        cost_usd = (
            usage.prompt_tokens * self._cfg.input_usd_per_mtok
            + usage.completion_tokens * self._cfg.output_usd_per_mtok
        ) / 1_000_000

        return CompletionResult(
            content=content,
            tool_calls=tuple(calls[i].to_request() for i in sorted(calls)),
            usage=usage,
            cost_usd=cost_usd,
            finish_reason=finish_reason,
        )
