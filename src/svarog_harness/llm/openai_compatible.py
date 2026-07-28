"""Openai-compatible реализация ModelProvider (единственная в MVP, ADR-0001).

Работает с любым сервером, говорящим на OpenAI chat completions API:
vLLM, llama.cpp, LiteLLM, OpenRouter, сам OpenAI. Всегда использует
streaming; retries и timeouts делегированы openai SDK.
"""

import base64
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from svarog_harness.config.schema import ModelsConfig, ProviderConfig
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ImageRef,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.llm.tool_call_leak import extract_leaked_tool_calls, leak_suspected
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
    models_cfg: ModelsConfig, store: SecretStore | None = None, workspace: Path | None = None
) -> "OpenAICompatibleProvider":
    """Провайдер для default-модели из конфигурации (валидность ссылки проверена схемой).

    `workspace` нужен, чтобы рендерить `ChatMessage.images` в части запроса
    (§3.10 native vision) — это провайдер основного agent loop.
    """
    return OpenAICompatibleProvider(
        models_cfg.providers[models_cfg.default], store=store, workspace=workspace
    )


def auxiliary_provider(
    models_cfg: ModelsConfig, store: SecretStore | None = None
) -> "OpenAICompatibleProvider":
    """Провайдер для auxiliary-модели: служебные задачи (curator слой 2, §13).

    Без workspace: служебные проходы (автозахват, curator) работают с текстом
    транскрипта, изображений не видят — при их появлении части image_url
    просто выродятся в текст «недоступно».
    """
    return OpenAICompatibleProvider(
        models_cfg.providers[models_cfg.auxiliary_or_default], store=store
    )


# Изображение стоит на порядок дороже своего описания, а история растёт.
# В запрос уходят только последние; более ранние вырождаются в текст —
# файл на месте, агент может перечитать (то же соображение, что за
# runtime.tool_output_context_chars).
MAX_IMAGES_IN_CONTEXT = 2


def _image_part(workspace: Path | None, ref: ImageRef) -> dict[str, Any]:
    """Часть запроса для изображения; недоступный файл — текстом, не исключением.

    `ref.path` не проверен: он приходит из checkpoint (JSON) или, для
    контейнеризованного агента, может оказаться абсолютным (`/workspace/...`,
    см. resolve_workspace_path в document_tools.py). `workspace / abs_path` в
    Python отдаёт `abs_path` как есть — join не спасает. Поэтому здесь
    отдельная проверка: путь после resolve() обязан остаться внутри
    workspace, иначе — деградация в текст, как при отсутствующем файле.
    Нормализация `/workspace/...` в относительный путь — забота места, где
    ImageRef создаётся (задача 4), не этого рендера.
    """
    if workspace is None:
        return {"type": "text", "text": f"изображение {ref.path} недоступно"}
    root = workspace.resolve()
    candidate = (root / ref.path).resolve()
    if not candidate.is_relative_to(root):
        # Абсолютный или выходящий через `..` путь: в запрос такое не уходит.
        return {"type": "text", "text": f"изображение {ref.path} недоступно"}
    try:
        data = base64.b64encode(candidate.read_bytes()).decode("ascii")
    except OSError:
        return {"type": "text", "text": f"изображение {ref.path} недоступно"}
    return {"type": "image_url", "image_url": {"url": f"data:{ref.mime};base64,{data}"}}


def _to_openai_messages(
    messages: list[ChatMessage], workspace: Path | None = None
) -> list[dict[str, Any]]:
    # Индексы сообщений, чьи изображения ещё попадут в запрос: считаем с
    # конца, чтобы вырождались старые, а не свежие.
    keep: set[int] = set()
    budget = MAX_IMAGES_IN_CONTEXT
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].images and budget > 0:
            keep.add(index)
            budget -= 1

    result: list[dict[str, Any]] = []
    for index, msg in enumerate(messages):
        item: dict[str, Any] = {"role": msg.role, "content": msg.content}
        if msg.images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": msg.content}]
            for ref in msg.images:
                parts.append(
                    _image_part(workspace, ref)
                    if index in keep
                    else {"type": "text", "text": f"изображение {ref.path} (показано ранее)"}
                )
            item["content"] = parts
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


def _cached_tokens(usage: Any) -> int:
    """Прочитать cached-токены из usage — диалекты провайдеров различаются.

    OpenAI/Qwen/Mistral/Zhipu кладут их в prompt_tokens_details.cached_tokens,
    StepFun/Moonshot — верхним уровнем, DeepSeek/SiliconFlow —
    в prompt_cache_hit_tokens.
    """
    details = getattr(usage, "prompt_tokens_details", None)
    for candidate in (
        getattr(details, "cached_tokens", None) if details is not None else None,
        getattr(usage, "cached_tokens", None),
        getattr(usage, "prompt_cache_hit_tokens", None),
    ):
        if isinstance(candidate, int) and candidate > 0:
            return candidate
    return 0


class _ToolCallAccumulator:
    """Сборка tool call из streaming-дельт: id/name приходят один раз, аргументы — кусками."""

    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments = ""

    def to_request(self) -> ToolCallRequest:
        # Сервер мог не прислать id (legacy function_call и небрежные реализации);
        # approval сопоставляется по call_id, поэтому id обязан быть уникальным.
        call_id = self.id or f"call-{uuid.uuid4().hex[:8]}"
        return ToolCallRequest(id=call_id, name=self.name, arguments_json=self.arguments)


class OpenAICompatibleProvider(ModelProvider):
    def __init__(
        self,
        cfg: ProviderConfig,
        *,
        client: AsyncOpenAI | None = None,
        store: SecretStore | None = None,
        workspace: Path | None = None,
    ) -> None:
        self._cfg = cfg
        # Корень для разрешения ImageRef.path в запросе (native vision);
        # без него изображения вырождаются в текст «недоступно».
        self._workspace = workspace
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
            "messages": _to_openai_messages(messages, self._workspace),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = _to_openai_tools(tools)

        stream = await self._client.chat.completions.create(**kwargs)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, _ToolCallAccumulator] = {}
        usage: Usage | None = None
        finish_reason: str | None = None

        async for chunk in stream:
            if chunk.usage is not None:
                usage = Usage(
                    prompt_tokens=chunk.usage.prompt_tokens,
                    completion_tokens=chunk.usage.completion_tokens,
                    cached_tokens=_cached_tokens(chunk.usage),
                )
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason is not None:
                finish_reason = choice.finish_reason
            delta = choice.delta
            if delta is None:
                continue
            # Диалекты канала рассуждений: OpenRouter/gpt-oss — `reasoning`,
            # DeepSeek — `reasoning_content`. Без их чтения ход из одних
            # рассуждений неотличим от пустого ответа.
            for attr in ("reasoning", "reasoning_content"):
                piece = getattr(delta, attr, None)
                if isinstance(piece, str) and piece:
                    reasoning_parts.append(piece)
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
            # Legacy-поле function_call: старые серверы шлют вызов через него,
            # игнорировать его — молча потерять вызов (индекс -1 не пересекается
            # с tool_calls, у которых index >= 0).
            legacy = getattr(delta, "function_call", None)
            if legacy is not None:
                acc = calls.setdefault(-1, _ToolCallAccumulator())
                if getattr(legacy, "name", None):
                    acc.name = legacy.name
                if getattr(legacy, "arguments", None):
                    acc.arguments += legacy.arguments

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        tool_calls = tuple(calls[i].to_request() for i in sorted(calls))
        suspected = False
        if not tool_calls and not content and leak_suspected(reasoning):
            # Вызов выронен в канал рассуждений (S19). Извлекать и ИСПОЛНЯТЬ
            # его нельзя: рассуждения — приватный черновик, модель могла вызов
            # лишь обдумывать. Только сигнал циклу — пусть попросит повторить
            # вызов штатным механизмом.
            suspected = True
        elif not tool_calls and content and leak_suspected(content):
            # Сервер не распарсил Harmony-вызов и отдал его текстом — извлекаем
            # сами; content при успехе отбрасываем (это внутренние каналы модели,
            # а не ответ пользователю). Текст уже ушёл в on_text_delta — терпимо.
            leaked = extract_leaked_tool_calls(content)
            if leaked:
                tool_calls = leaked
                content = ""
            else:
                suspected = True
        if usage is None:
            prompt_text = "".join(m.content for m in messages)
            # Оценка по сырому выводу модели — до отбрасывания протёкшего content.
            completion_text = "".join(content_parts) + "".join(a.arguments for a in calls.values())
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
            tool_calls=tool_calls,
            usage=usage,
            cost_usd=cost_usd,
            finish_reason=finish_reason,
            reasoning=reasoning,
            leak_suspected=suspected,
        )
