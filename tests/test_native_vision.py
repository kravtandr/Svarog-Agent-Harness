"""Изображения в нативном цикле: ссылки, рендер, лимит (план 2026-07-28)."""

import base64
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.schema import (
    AutonomyMode,
    PoliciesConfig,
    ProviderConfig,
    RuntimeConfig,
    SvarogConfig,
)
from svarog_harness.llm.openai_compatible import OpenAICompatibleProvider, _to_openai_messages
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ImageRef,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.policy.engine import PolicyEngine
from svarog_harness.runtime.checkpoint import LoopState, _message_from_dict, _message_to_dict
from svarog_harness.runtime.loop import AgentLoop
from svarog_harness.runtime.orchestrator import TaskRunner
from svarog_harness.sandbox.local import LocalEnvironment
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import Checkpoint, RunState
from svarog_harness.tools.base import ToolResult
from svarog_harness.tools.document_tools import ReadImageArgs, ReadImageTool
from svarog_harness.tools.registry import ToolRegistry
from svarog_harness.trace.recorder import TraceRecorder


def test_message_without_images_round_trips_unchanged() -> None:
    message = ChatMessage(role="tool", content="готово", tool_call_id="c1")
    raw = _message_to_dict(message)
    assert raw["images"] == []
    assert _message_from_dict(raw) == message


def test_checkpoint_keeps_reference_not_bytes() -> None:
    message = ChatMessage(
        role="user",
        content="Изображение из read_image:",
        images=(ImageRef(path=".attachments/ab_shot.png", mime="image/png"),),
    )

    raw = _message_to_dict(message)

    assert raw["images"] == [{"path": ".attachments/ab_shot.png", "mime": "image/png"}]
    assert "data" not in str(raw), "в checkpoint не должно быть base64"
    assert _message_from_dict(raw) == message


def test_old_checkpoint_without_images_key_still_loads() -> None:
    """Строки, записанные до этой работы, обязаны читаться."""
    raw = {"role": "user", "content": "текст", "tool_calls": [], "tool_call_id": None}
    assert _message_from_dict(raw).images == ()


@pytest.mark.asyncio
async def test_image_block_carries_its_path(tmp_path) -> None:
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    result = await ReadImageTool(tmp_path).execute(ReadImageArgs(path="shot.png"))

    assert result.ok
    block = result.blocks[0]
    assert block["path"] == "shot.png"
    assert block["mimeType"] == "image/png"
    assert block["data"], "base64 на месте"


def test_bridge_strips_path_from_mcp_blocks() -> None:
    """MCP-потребитель не должен видеть наш служебный ключ."""
    from svarog_harness.runtime.bridge_control import _mcp_blocks

    cleaned = _mcp_blocks(
        [{"type": "image", "data": "AA", "mimeType": "image/png", "path": "a.png"}]
    )

    assert cleaned == [{"type": "image", "data": "AA", "mimeType": "image/png"}]


def test_message_without_images_stays_a_plain_string(tmp_path: Path) -> None:
    """Обратная совместимость: все существующие вызовы не должны измениться."""
    rendered = _to_openai_messages([ChatMessage(role="user", content="привет")], tmp_path)
    assert rendered == [{"role": "user", "content": "привет"}]


def test_image_becomes_a_data_uri_part(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(b"\x89PNG")
    message = ChatMessage(
        role="user",
        content="смотри:",
        images=(ImageRef(path="shot.png", mime="image/png"),),
    )

    rendered = _to_openai_messages([message], tmp_path)

    parts = rendered[0]["content"]
    assert parts[0] == {"type": "text", "text": "смотри:"}
    expected = base64.b64encode(b"\x89PNG").decode("ascii")
    assert parts[1] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{expected}"},
    }


def test_missing_file_degrades_to_text_instead_of_raising(tmp_path: Path) -> None:
    message = ChatMessage(
        role="user", content="смотри:", images=(ImageRef(path="нет.png", mime="image/png"),)
    )

    parts = _to_openai_messages([message], tmp_path)[0]["content"]

    assert all(p["type"] == "text" for p in parts)
    assert "недоступно" in parts[1]["text"]


def test_only_two_newest_images_are_sent(tmp_path: Path) -> None:
    for name in ("a.png", "b.png", "c.png"):
        (tmp_path / name).write_bytes(b"\x89PNG")
    messages = [
        ChatMessage(role="user", content=f"{n}:", images=(ImageRef(path=n, mime="image/png"),))
        for n in ("a.png", "b.png", "c.png")
    ]

    rendered = _to_openai_messages(messages, tmp_path)

    kinds = [[p["type"] for p in item["content"]] for item in rendered]
    assert kinds[0] == ["text", "text"], "самое старое выродилось в текст"
    assert kinds[1] == ["text", "image_url"]
    assert kinds[2] == ["text", "image_url"]
    assert "показано ранее" in rendered[0]["content"][1]["text"]


def test_without_workspace_images_degrade_to_text() -> None:
    """Вызов без workspace (внешние потребители) не должен падать."""
    message = ChatMessage(
        role="user", content="x", images=(ImageRef(path="a.png", mime="image/png"),)
    )
    parts = _to_openai_messages([message], None)[0]["content"]
    assert all(p["type"] == "text" for p in parts)
    assert "недоступно" in parts[1]["text"]


def test_absolute_image_path_degrades_to_text_instead_of_escaping_workspace(
    tmp_path: Path,
) -> None:
    """`workspace / '/etc/passwd'` в Python побеждает абсолютный путь — не должно читаться.

    Контейнеризованный агент может записать в ImageRef абсолютный путь вида
    `/workspace/shot.png` (см. resolve_workspace_path в document_tools.py); такой
    путь обязан вырождаться в текст, а не резолвиться в хост-путь вне workspace.
    """
    outside = tmp_path.parent / "outside_shot.png"
    outside.write_bytes(b"\x89PNG")
    message = ChatMessage(
        role="user", content="смотри:", images=(ImageRef(path=str(outside), mime="image/png"),)
    )

    parts = _to_openai_messages([message], tmp_path)[0]["content"]

    assert all(p["type"] == "text" for p in parts)
    assert "недоступно" in parts[1]["text"]


def test_path_traversal_degrades_to_text_instead_of_escaping_workspace(tmp_path: Path) -> None:
    """`../` в ImageRef.path не должен выводить чтение за пределы workspace."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside_shot.png"
    outside.write_bytes(b"\x89PNG")
    message = ChatMessage(
        role="user",
        content="смотри:",
        images=(ImageRef(path="../outside_shot.png", mime="image/png"),),
    )

    parts = _to_openai_messages([message], workspace)[0]["content"]

    assert all(p["type"] == "text" for p in parts)
    assert "недоступно" in parts[1]["text"]


# --- AgentLoop._image_refs (задача 4) ---------------------------------------


def test_image_refs_read_from_blocks() -> None:
    result = ToolResult(
        ok=True,
        output="изображение shot.png",
        blocks=[{"type": "image", "data": "AA", "mimeType": "image/png", "path": "shot.png"}],
    )

    refs = AgentLoop._image_refs(result)

    assert refs == (ImageRef(path="shot.png", mime="image/png"),)


def test_blocks_without_path_are_ignored() -> None:
    """Блок из чужого источника без пути не должен ронять цикл."""
    result = ToolResult(ok=True, output="x", blocks=[{"type": "image", "data": "AA"}])
    assert AgentLoop._image_refs(result) == ()


def test_non_image_result_gives_no_refs() -> None:
    assert AgentLoop._image_refs(ToolResult.success("просто текст")) == ()


def test_container_workspace_prefix_is_normalized() -> None:
    """Агент в контейнере видит workspace как /workspace — путь схлопывается в относительный."""
    result = ToolResult(
        ok=True,
        output="x",
        blocks=[
            {
                "type": "image",
                "data": "AA",
                "mimeType": "image/png",
                "path": "/workspace/shot.png",
            }
        ],
    )

    assert AgentLoop._image_refs(result) == (ImageRef(path="shot.png", mime="image/png"),)


def test_path_escaping_workspace_yields_no_ref() -> None:
    """`../secret.png` не нормализуется в безопасный путь — ссылка отбрасывается целиком."""
    result = ToolResult(
        ok=True,
        output="x",
        blocks=[{"type": "image", "data": "AA", "mimeType": "image/png", "path": "../secret.png"}],
    )

    assert AgentLoop._image_refs(result) == ()


class _ScriptedProvider(ModelProvider):
    """Провайдер-заглушка по образцу ScriptedProvider из test_approval_flow.py:
    ходы заданы заранее и отдаются по очереди, без обращения к реальной модели.

    Расширено в задаче 7: запоминает `messages` каждого хода. AgentLoop работает
    с ChatMessage/ImageRef, а не с телом запроса — чтобы увидеть то, что реально
    получил бы клиент, нужно прогнать сохранённую историю через
    `_to_openai_messages` отдельно (см. test_image_reaches_the_request_as_a_data_uri)."""

    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)
        self.calls: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.calls.append(list(messages))
        return self.turns.pop(0)


@pytest.fixture
async def _vision_db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_tool_message_precedes_the_image_message(
    tmp_path: Path, _vision_db: AsyncSession
) -> None:
    """Порядок обязателен: без tool-ответа ход остаётся без ответа на tool_call_id."""
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    provider = _ScriptedProvider(
        [
            CompletionResult(
                content="",
                tool_calls=(
                    ToolCallRequest(
                        id="c1",
                        name="read_image",
                        arguments_json=json.dumps({"path": "shot.png"}),
                    ),
                ),
                usage=Usage(10, 5),
                finish_reason="tool_calls",
            ),
            CompletionResult(content="вижу изображение", usage=Usage(10, 5), finish_reason="stop"),
        ]
    )
    registry = ToolRegistry()
    registry.register(ReadImageTool(tmp_path))
    loop = AgentLoop(
        provider,
        registry,
        TraceRecorder(_vision_db),
        RuntimeConfig(),
        PolicyEngine(
            autonomy=AutonomyMode.SUPERVISED, policies=PoliciesConfig(), workspace=tmp_path
        ),
        tmp_path,
        model_name="test-model",
    )

    outcome = await loop.run("покажи картинку", AutonomyMode.SUPERVISED)

    assert outcome.state is RunState.COMPLETED

    checkpoint = (
        await _vision_db.execute(
            select(Checkpoint)
            .where(Checkpoint.run_id == outcome.run_id)
            .order_by(Checkpoint.iteration.desc(), Checkpoint.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    state = LoopState.from_dict(checkpoint.state)

    roles = [(m.role, bool(m.images)) for m in state.messages]
    tool_at = roles.index(("tool", False))
    assert roles[tool_at + 1] == ("user", True), (
        "за tool-сообщением должно сразу идти user-сообщение с непустыми images"
    )


# --- Задача 7: сквозной прогон ------------------------------------------------


@pytest.mark.asyncio
async def test_image_reaches_the_request_as_a_data_uri(
    tmp_path: Path, _vision_db: AsyncSession
) -> None:
    """Файл → read_image → история → запрос. Всё остальное — рассуждение."""
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    provider = _ScriptedProvider(
        [
            CompletionResult(
                content="",
                tool_calls=(
                    ToolCallRequest(
                        id="c1",
                        name="read_image",
                        arguments_json=json.dumps({"path": "shot.png"}),
                    ),
                ),
                usage=Usage(10, 5),
                finish_reason="tool_calls",
            ),
            CompletionResult(content="вижу изображение", usage=Usage(10, 5), finish_reason="stop"),
        ]
    )
    registry = ToolRegistry()
    registry.register(ReadImageTool(tmp_path))
    loop = AgentLoop(
        provider,
        registry,
        TraceRecorder(_vision_db),
        RuntimeConfig(),
        PolicyEngine(
            autonomy=AutonomyMode.SUPERVISED, policies=PoliciesConfig(), workspace=tmp_path
        ),
        tmp_path,
        model_name="test-model",
    )

    outcome = await loop.run("покажи картинку", AutonomyMode.SUPERVISED)

    assert outcome.state is RunState.COMPLETED

    # Второй ход провайдера — тот, где в историю уже попали tool-ответ и
    # user-сообщение с ImageRef. Прогоняем его через настоящий
    # _to_openai_messages: это и есть то, что увидел бы реальный клиент —
    # не state.messages (там всё ещё ссылка на файл, а не base64).
    rendered = _to_openai_messages(provider.calls[1], tmp_path)

    tool_index = next(i for i, m in enumerate(rendered) if m["role"] == "tool")
    assert rendered[tool_index]["tool_call_id"] == "c1"

    user_message = rendered[tool_index + 1]
    assert user_message["role"] == "user"
    assert isinstance(user_message["content"], list), "содержимое обязано быть массивом частей"

    image_part = next(p for p in user_message["content"] if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


# --- Пояснение при отказе провайдера (задача 5) ------------------------------


class _FailingClient:
    """Дублирует client.chat.completions.create, но всегда бросает — по образцу
    _FakeClient из test_llm.py, только имитирует отказ провайдера, а не ответ."""

    def __init__(self, exc: Exception) -> None:
        async def create(**kwargs: Any) -> Any:
            raise exc

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


@pytest.mark.asyncio
async def test_provider_error_gains_a_vision_hint_only_when_images_were_sent(
    tmp_path: Path,
) -> None:
    """Без изображений пояснение вводило бы в заблуждение."""
    cfg = ProviderConfig(base_url="http://localhost:8000/v1", model="test-model")
    (tmp_path / "shot.png").write_bytes(b"\x89PNG")

    with_image = OpenAICompatibleProvider(
        cfg,
        client=cast(AsyncOpenAI, _FailingClient(ValueError("400 Bad Request"))),
        workspace=tmp_path,
    )
    message_with_image = ChatMessage(
        role="user", content="смотри:", images=(ImageRef(path="shot.png", mime="image/png"),)
    )
    with pytest.raises(RuntimeError, match="изображение") as with_image_exc:
        await with_image.complete([message_with_image], [])
    assert "400 Bad Request" in str(with_image_exc.value), "исходная ошибка не должна теряться"

    text_only = OpenAICompatibleProvider(
        cfg, client=cast(AsyncOpenAI, _FailingClient(ValueError("400 Bad Request")))
    )
    with pytest.raises(ValueError) as text_only_exc:
        await text_only.complete([ChatMessage(role="user", content="привет")], [])
    assert "изображение" not in str(text_only_exc.value)


@pytest.mark.asyncio
async def test_provider_error_has_no_hint_when_image_degraded_to_text(tmp_path: Path) -> None:
    """ChatMessage.images непуст, но без workspace часть вырождается в текст (§ провайдер
    auxiliary): реального изображения в запросе нет, значит и подсказки быть не должно."""
    cfg = ProviderConfig(base_url="http://localhost:8000/v1", model="test-model")
    provider = OpenAICompatibleProvider(
        cfg, client=cast(AsyncOpenAI, _FailingClient(ValueError("400 Bad Request")))
    )
    message = ChatMessage(
        role="user", content="смотри:", images=(ImageRef(path="shot.png", mime="image/png"),)
    )

    with pytest.raises(ValueError) as exc:
        await provider.complete([message], [])

    assert "изображение" not in str(exc.value)


# --- регистрация в нативном цикле (задача 6) --------------------------------


def _build_registry(tmp_path: Path) -> ToolRegistry:
    """Собрать дефолтный реестр нативного цикла — оснастка по образцу
    tests/test_dream_profile.py (`_runner`/`_names`)."""
    cfg = SvarogConfig.model_validate(
        {
            "models": {
                "default": "main",
                "providers": {"main": {"base_url": "http://localhost", "model": "m"}},
            },
            "memory": {"path": str(tmp_path / "memory")},
            "storage": {"db_path": str(tmp_path / "svarog.sqlite3")},
        }
    )
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    runner = TaskRunner(cfg, tmp_path)
    # Сборка реестра живёт в RunAssembly (run_assembly.py); TaskRunner держит
    # её в _assembly и наружу приватным методом не выставляет.
    return runner._assembly._build_registry(
        LocalEnvironment(tmp_path),
        [],
        [],
        [],
        [],
        None,
        None,
        mem_dir=tmp_path / "memory",
        memory_proposal_sink=[],
    )


def test_read_image_is_registered_in_the_native_loop(tmp_path: Path) -> None:
    """Без этого весь путь вложений упирается в «нет инструмента для картинок»."""
    registry = _build_registry(tmp_path)
    assert "read_image" in registry.names()


def test_read_document_registered_only_when_markitdown_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "svarog_harness.runtime.run_assembly.document_tools_available", lambda: False
    )
    registry = _build_registry(tmp_path)
    assert "read_document" not in registry.names()
    assert "read_image" in registry.names(), "картинки от markitdown не зависят"
