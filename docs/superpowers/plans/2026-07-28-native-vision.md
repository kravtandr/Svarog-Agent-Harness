# Нативный цикл видит изображения — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** прикреплённый скриншот доходит до модели в нативном цикле, а документы читаются `read_document` — без внешнего агента.

**Architecture:** `ChatMessage` учится нести **ссылки** на изображения в workspace, а не байты: `LoopState` сериализуется в checkpoint после каждого вызова инструмента, и base64 там стоил бы мегабайты на каждое сохранение. Base64 материализуется один раз, при сборке запроса к провайдеру. Доставка — отдельным сообщением роли `user` следом за tool-ответом, потому что openai-совместимый контракт принимает части `image_url` только у `user`.

**Tech Stack:** Python 3.12, Pydantic v2, openai SDK, SQLAlchemy async.

Спек: `docs/superpowers/specs/2026-07-28-native-vision-design.md`.

## Global Constraints

- Ветка: работа начинается с `feat/composer-completion` (она ещё не влита). Первая задача заводит `feat/native-vision` от неё.
- Комментарии и тексты ошибок — по-русски, как в окружающем коде.
- В checkpoint не попадают байты изображений — только относительный путь и mime.
- Обратная совместимость: сообщение без изображений сериализуется и рендерится ровно как раньше; все существующие вызовы провайдера не меняются.
- Гейты, все зелёные перед коммитом:
  - `COLUMNS=200 .venv/bin/python -m pytest -q`
  - `.venv/bin/ruff check src tests` и `.venv/bin/ruff format --check .`
  - `.venv/bin/mypy`
- Известные предсуществующие флаки под полной нагрузкой, проходящие изолированно: `test_cancel_running_cooperative` и один в `test_cloud_sessions.py` (`database is locked`). Упало — перезапустить изолированно, подтвердить, не гоняться.
- В отчётах вывод терминала вставляется дословно и целиком: команда, баннер инструмента, блок падения, счётчики, хвостовые строки длительности.

## Структура файлов

| Файл | Что меняется |
|---|---|
| `src/svarog_harness/llm/provider.py` | `ImageRef`, поле `ChatMessage.images` |
| `src/svarog_harness/runtime/checkpoint.py:130-144` | Сериализация `images` без байтов |
| `src/svarog_harness/tools/document_tools.py:190-193` | Блок изображения несёт `path` |
| `src/svarog_harness/runtime/bridge_control.py:263-266` | `path` снимается перед отдачей в MCP |
| `src/svarog_harness/llm/openai_compatible.py:67-83` | Рендер частей `image_url`, лимит, ошибка про vision |
| `src/svarog_harness/runtime/loop.py:536, 621` | User-сообщение с изображениями следом за tool-ответом |
| `src/svarog_harness/runtime/run_assembly.py:524` | Регистрация `ReadImageTool` и `ReadDocumentTool` |
| `tests/test_native_vision.py` | Новый файл под всё перечисленное |

---

### Задача 1: `ImageRef` и `ChatMessage.images`

**Files:**
- Modify: `src/svarog_harness/llm/provider.py:51-63`
- Modify: `src/svarog_harness/runtime/checkpoint.py:130-144`
- Test: `tests/test_native_vision.py`

**Interfaces:**
- Produces: `ImageRef` (frozen dataclass, поля `path: str`, `mime: str`); `ChatMessage.images: tuple[ImageRef, ...] = ()`.

- [ ] **Шаг 1: завести ветку**

```bash
git checkout -b feat/native-vision
```

- [ ] **Шаг 2: тест**

```python
"""Изображения в нативном цикле: ссылки, рендер, лимит (план 2026-07-28)."""

from svarog_harness.llm.provider import ChatMessage, ImageRef
from svarog_harness.runtime.checkpoint import _message_from_dict, _message_to_dict


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
```

- [ ] **Шаг 3: убедиться, что падают**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_native_vision.py -q`
Expected: FAIL — `ImportError: cannot import name 'ImageRef'`

- [ ] **Шаг 4: реализация**

В `provider.py`, перед `ChatMessage`:

```python
@dataclass(frozen=True)
class ImageRef:
    """Ссылка на изображение в workspace, а не его байты.

    `LoopState.messages` сериализуется в checkpoint после каждого вызова
    инструмента: пятимегабайтная картинка — это ~6.7 MB base64 в каждом
    сохранении. Байты читаются один раз, при сборке запроса к провайдеру.
    """

    path: str  # относительно workspace run'а
    mime: str
```

`ChatMessage` получает `images: tuple[ImageRef, ...] = ()`.

В `checkpoint.py`:

```python
def _message_to_dict(message: ChatMessage) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_calls": [_call_to_dict(c) for c in message.tool_calls],
        "tool_call_id": message.tool_call_id,
        "images": [{"path": i.path, "mime": i.mime} for i in message.images],
    }


def _message_from_dict(raw: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        role=raw["role"],
        content=raw["content"],
        tool_calls=tuple(_call_from_dict(c) for c in raw["tool_calls"]),
        tool_call_id=raw["tool_call_id"],
        # .get, а не [...]: checkpoint'ы, записанные до этой работы, ключа не несут.
        images=tuple(
            ImageRef(path=str(i["path"]), mime=str(i["mime"])) for i in raw.get("images", [])
        ),
    )
```

- [ ] **Шаг 5: тесты зелёные и коммит**

```bash
COLUMNS=200 .venv/bin/python -m pytest tests/test_native_vision.py -q
git add src tests && git commit -m "feat(llm): ChatMessage несёт ссылки на изображения"
```

---

### Задача 2: блок изображения знает свой путь

`ReadImageTool` отдаёт base64 и mime, но не путь — а именно путь нужен циклу, чтобы построить `ImageRef`. Читать его из аргументов вызова значило бы связать цикл со схемой конкретного инструмента.

**Files:**
- Modify: `src/svarog_harness/tools/document_tools.py:190-193`
- Modify: `src/svarog_harness/runtime/bridge_control.py:263-266`
- Test: `tests/test_native_vision.py`

**Interfaces:**
- Produces: блок изображения вида `{"type": "image", "data": …, "mimeType": …, "path": …}`; мост снимает `path` перед отдачей в MCP.

- [ ] **Шаг 1: тесты**

```python
import pytest

from svarog_harness.tools.document_tools import ReadImageTool
from svarog_harness.tools.document_tools import ReadImageArgs


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

    cleaned = _mcp_blocks([{"type": "image", "data": "AA", "mimeType": "image/png", "path": "a.png"}])

    assert cleaned == [{"type": "image", "data": "AA", "mimeType": "image/png"}]
```

- [ ] **Шаг 2: убедиться, что падают**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_native_vision.py -q -k image_block or bridge_strips`
Expected: FAIL — в блоке нет `path`; `_mcp_blocks` не существует

- [ ] **Шаг 3: реализация**

`document_tools.py`, в `ReadImageTool.execute`:

```python
        return ToolResult(
            ok=True,
            output=f"изображение {args.path} ({mime}, {size} байт)",
            # path — для нативного цикла: по нему строится ImageRef, который
            # переживает checkpoint вместо base64. Мост снимает ключ перед
            # отдачей блока в MCP.
            blocks=[{"type": "image", "data": data, "mimeType": mime, "path": args.path}],
        )
```

`bridge_control.py`, рядом с местом, где блоки уходят наружу:

```python
def _mcp_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Блоки для MCP-потребителя без наших служебных ключей."""
    return [{k: v for k, v in block.items() if k != "path"} for block in blocks]
```

и в теле — `return _mcp_blocks(result.blocks), False`.

- [ ] **Шаг 4: тесты зелёные и коммит**

```bash
COLUMNS=200 .venv/bin/python -m pytest tests/test_native_vision.py tests/test_document_tools.py tests/test_bridge.py -q
git add src tests && git commit -m "feat(tools): блок изображения несёт путь, мост его снимает"
```

---

### Задача 3: рендер частей `image_url`

**Files:**
- Modify: `src/svarog_harness/llm/openai_compatible.py:67-83`
- Test: `tests/test_native_vision.py`

**Interfaces:**
- Consumes: `ImageRef`, `ChatMessage.images` (задача 1).
- Produces: `_to_openai_messages(messages, workspace: Path | None = None)`; константа `MAX_IMAGES_IN_CONTEXT = 2`.

- [ ] **Шаг 1: тесты**

```python
import base64
from pathlib import Path

from svarog_harness.llm.openai_compatible import _to_openai_messages


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
```

- [ ] **Шаг 2: убедиться, что падают**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_native_vision.py -q -k openai or image_url or two_newest`
Expected: FAIL — `_to_openai_messages` принимает один аргумент

- [ ] **Шаг 3: реализация**

```python
# Изображение стоит на порядок дороже своего описания, а история растёт.
# В запрос уходят только последние; более ранние вырождаются в текст —
# файл на месте, агент может перечитать (то же соображение, что за
# runtime.tool_output_context_chars).
MAX_IMAGES_IN_CONTEXT = 2


def _image_part(workspace: Path | None, ref: ImageRef) -> dict[str, Any]:
    """Часть запроса для изображения; недоступный файл — текстом, не исключением."""
    if workspace is None:
        return {"type": "text", "text": f"изображение {ref.path} недоступно"}
    path = workspace / ref.path
    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
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
```

Найти вызов `_to_openai_messages` в `OpenAICompatibleProvider.complete` и передать туда workspace. Провайдер конструируется без него — добавить необязательный параметр конструктора `workspace: Path | None = None` и сохранить в поле; `RunAssembly` передаёт `self._workspace` при создании провайдера. Найти, где провайдер создаётся (`default_provider`, `auxiliary_provider` в `openai_compatible.py`), и протянуть параметр.

- [ ] **Шаг 4: тесты зелёные и коммит**

```bash
COLUMNS=200 .venv/bin/python -m pytest tests/test_native_vision.py -q
git add src tests && git commit -m "feat(llm): изображения рендерятся частями image_url"
```

---

### Задача 4: цикл кладёт изображение отдельным сообщением

**Files:**
- Modify: `src/svarog_harness/runtime/loop.py:536, 621`
- Test: `tests/test_native_vision.py`

**Interfaces:**
- Consumes: `ImageRef`, `ChatMessage.images` (задача 1); блок с `path` (задача 2).
- Produces: приватный метод `AgentLoop._image_refs(result) -> tuple[ImageRef, ...]`.

- [ ] **Шаг 1: тесты**

```python
from svarog_harness.runtime.loop import AgentLoop
from svarog_harness.tools.base import ToolResult


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
```

Плюс тест на порядок сообщений — он требует прогона цикла. Взять оснастку с фальшивым провайдером из `tests/test_approval_flow.py` (там `ModelProvider` реализуется классом-заглушкой) и проверить, что после вызова `read_image` в `state.messages` подряд идут `role="tool"` и `role="user"` с непустым `images`, именно в этом порядке:

```python
@pytest.mark.asyncio
async def test_tool_message_precedes_the_image_message(tmp_path: Path) -> None:
    """Порядок обязателен: без tool-ответа ход остаётся без ответа на tool_call_id."""
    # Оснастка: провайдер, который первым ходом зовёт read_image, вторым отвечает текстом.
    # Реализуется по образцу ScriptedProvider из tests/test_approval_flow.py.
    ...
```

Оснастку написать по образцу; многоточие в плане — единственное место, и оно намеренно: форма заглушки зависит от текущего вида `ModelProvider`, который надо прочитать. Тест обязан существовать и проверять именно порядок двух сообщений.

- [ ] **Шаг 2: убедиться, что падают**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_native_vision.py -q -k image_refs`
Expected: FAIL — у `AgentLoop` нет `_image_refs`

- [ ] **Шаг 3: реализация**

```python
    @staticmethod
    def _image_refs(result: ToolResult) -> tuple[ImageRef, ...]:
        """Ссылки на изображения из блоков результата; чужие блоки игнорируются."""
        refs: list[ImageRef] = []
        for block in result.blocks or ():
            if block.get("type") != "image":
                continue
            path, mime = block.get("path"), block.get("mimeType")
            if isinstance(path, str) and isinstance(mime, str):
                refs.append(ImageRef(path=path, mime=mime))
        return tuple(refs)
```

В обоих местах, где добавляется tool-сообщение (строки ~536 и ~621), после него:

```python
            images = self._image_refs(tool_result)
            if images:
                # Отдельным user-сообщением, а не в tool-ответе: openai-совместимый
                # контракт принимает части image_url только у роли user. Порядок
                # обязателен — без tool-ответа ход остаётся без ответа на tool_call_id.
                note = ChatMessage(
                    role="user", content=f"Изображение из {call.name}:", images=images
                )
                state.messages.append(note)
                await self._record_message(run, "user", {"content": note.content})
```

Во втором месте вместо `call` — `prepared.call`.

- [ ] **Шаг 4: тесты зелёные и коммит**

```bash
COLUMNS=200 .venv/bin/python -m pytest tests/test_native_vision.py tests/test_loop.py -q
git add src tests && git commit -m "feat(runtime): изображение уходит отдельным user-сообщением"
```

---

### Задача 5: пояснение при отказе провайдера

**Files:**
- Modify: `src/svarog_harness/llm/openai_compatible.py`
- Test: `tests/test_native_vision.py`

**Interfaces:**
- Consumes: `MAX_IMAGES_IN_CONTEXT`, рендер частей (задача 3).

- [ ] **Шаг 1: тест**

```python
@pytest.mark.asyncio
async def test_provider_error_gains_a_vision_hint_only_when_images_were_sent(tmp_path) -> None:
    """Без изображений пояснение вводило бы в заблуждение."""
    # Провайдер, чей клиент всегда бросает; проверяем текст поднятой ошибки.
    # Оснастка — подменённый AsyncOpenAI через monkeypatch на атрибуте провайдера.
    ...
```

Оснастку написать по образцу существующих тестов провайдера (`tests/test_llm_*.py` — прочитать, какой там способ подмены клиента). Тест обязан проверять оба случая: с изображениями пояснение есть, без изображений его нет.

- [ ] **Шаг 2: убедиться, что падает**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_native_vision.py -q -k vision_hint`
Expected: FAIL — пояснения нет

- [ ] **Шаг 3: реализация**

Обернуть вызов клиента в `complete`:

```python
        had_images = any(m.images for m in messages)
        try:
            ...  # существующий вызов
        except Exception as exc:
            if had_images:
                # Надёжного признака поддержки vision у произвольного
                # openai-совместимого endpoint нет; гадать по имени модели
                # хуже, чем честно объяснить отказ.
                raise RuntimeError(
                    f"{exc}. В запросе было изображение — возможно, модель его не принимает: "
                    f"попробуйте другую модель или внешнего агента"
                ) from exc
            raise
```

Точный тип существующего исключения и место вызова уточнить по коду `complete`.

- [ ] **Шаг 4: тесты зелёные и коммит**

```bash
COLUMNS=200 .venv/bin/python -m pytest tests/test_native_vision.py -q
git add src tests && git commit -m "feat(llm): понятный отказ, когда модель не приняла изображение"
```

---

### Задача 6: регистрация инструментов в нативном цикле

**Files:**
- Modify: `src/svarog_harness/runtime/run_assembly.py:524`
- Test: `tests/test_native_vision.py`

**Interfaces:**
- Consumes: ничего из предыдущих задач.

- [ ] **Шаг 1: тесты**

```python
def test_read_image_is_registered_in_the_native_loop(tmp_path: Path) -> None:
    """Без этого весь путь вложений упирается в «нет инструмента для картинок»."""
    registry = _build_registry(tmp_path)  # оснастка по образцу tests/test_registry.py
    assert "read_image" in registry.names()


def test_read_document_registered_only_when_markitdown_present(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "svarog_harness.runtime.run_assembly.document_tools_available", lambda: False
    )
    registry = _build_registry(tmp_path)
    assert "read_document" not in registry.names()
    assert "read_image" in registry.names(), "картинки от markitdown не зависят"
```

Оснастку `_build_registry` написать по образцу существующих тестов сборки (прочитать `tests/test_registry.py` или ближайший, где реестр уже строится); точное имя метода перечисления инструментов уточнить по `ToolRegistry`.

- [ ] **Шаг 2: убедиться, что падают**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_native_vision.py -q -k registered`
Expected: FAIL — `read_image` в реестре нет

- [ ] **Шаг 3: реализация**

В `run_assembly.py`, рядом с `for tool in file_tools(...)`:

```python
        # Картинки и документы — те же инструменты, что у внешнего агента
        # (bridge_control.py). read_image зависимостей не имеет; read_document
        # требует опциональной группы docs.
        registry.register(ReadImageTool(self._workspace))
        if document_tools_available():
            registry.register(ReadDocumentTool(self._workspace))
```

Импорты — из `svarog_harness.tools.document_tools`.

- [ ] **Шаг 4: тесты зелёные и коммит**

```bash
COLUMNS=200 .venv/bin/python -m pytest -q
git add src tests && git commit -m "feat(runtime): read_image и read_document в нативном цикле"
```

---

### Задача 7: сквозной прогон

Единственный тест, доказывающий, что картинка проходит весь путь: файл → инструмент → история → запрос.

**Files:**
- Test: `tests/test_native_vision.py`

- [ ] **Шаг 1: тест**

```python
@pytest.mark.asyncio
async def test_image_reaches_the_request_as_a_data_uri(tmp_path: Path) -> None:
    """Файл → read_image → история → запрос. Всё остальное — рассуждение."""
    # Провайдер-заглушка сохраняет messages, которые ему передали, и первым
    # ходом зовёт read_image('shot.png'), вторым отвечает текстом.
    # Прогнать AgentLoop, затем проверить в сохранённом запросе:
    #   - есть сообщение role="user" с content-массивом,
    #   - в нём часть image_url с url, начинающимся на "data:image/png;base64,",
    #   - предыдущее сообщение имеет role="tool" и тот же tool_call_id.
    ...
```

Оснастка — по образцу задачи 4; если она уже написана там, переиспользовать. Многоточие намеренно: форма заглушки зависит от прочитанного `ModelProvider`. Тест обязан существовать и проверять все три утверждения.

- [ ] **Шаг 2: прогнать**

Run: `COLUMNS=200 .venv/bin/python -m pytest tests/test_native_vision.py -q`
Expected: PASS — реализация из задач 1-6 уже должна это обеспечивать. FAIL — чинить реализацию, не тест.

- [ ] **Шаг 3: доказать невакуумность**

Временно вернуть `_to_openai_messages` к рендеру строкой (убрать ветку `if msg.images`), убедиться, что тест краснеет, восстановить. Вставить в отчёт оба вывода целиком.

- [ ] **Шаг 4: все гейты и коммит**

```bash
COLUMNS=200 .venv/bin/python -m pytest -q && .venv/bin/ruff check src tests && .venv/bin/ruff format --check . && .venv/bin/mypy
git add tests && git commit -m "test(runtime): изображение доходит до запроса data-URI"
```

---

### Задача 8: спек и живая проверка

**Files:**
- Modify: `docs/superpowers/specs/2026-07-28-composer-completion-and-uploads-design.md`

- [ ] **Шаг 1: снять оговорку**

В разделе «Что не делается» этого спека записано, что мультимодальность в LLM-слое не трогается и картинка доходит через `read_image`. Первое перестало быть правдой: `ChatMessage` теперь несёт изображения. Переписать пункт: путь к модели по-прежнему через `read_image`, но результат инструмента доезжает до модели картинкой, а не только текстом; отдельной строкой — что это работает и на нативном цикле.

- [ ] **Шаг 2: все гейты**

```bash
COLUMNS=200 .venv/bin/python -m pytest -q \
  && .venv/bin/ruff check src tests && .venv/bin/ruff format --check . \
  && .venv/bin/mypy \
  && npm --prefix web test && npm --prefix web run build
```

- [ ] **Шаг 3: живая проверка**

Поднять `svarog serve` из проектной папки с `executor: native` и vision-моделью, вставить скриншот в поле ввода, отправить и убедиться, что агент описывает изображение, а не отвечает «нет инструмента для чтения изображений».

- [ ] **Шаг 4: коммит**

```bash
git add docs && git commit -m "docs: нативный цикл видит изображения"
```

---

## Самопроверка плана

**Покрытие спека.** §1 `ImageRef` и поле — задача 1. §2 доставка user-сообщением — задача 4. §3 рендер и пропавший файл — задача 3. §4 лимит двух изображений — задача 3 (тот же код, разделять незачем). §5 отказ провайдера — задача 5. §6 регистрация — задача 6. Таблица ошибок: пропавший файл — задача 3; отказ провайдера — задача 5; лимит 5 MB — существующее поведение `read_image`, тестом не дублируется; отсутствие markitdown — задача 6.

**Три многоточия, все намеренные и объяснённые.** Задачи 4, 5 и 7 требуют заглушки провайдера, форма которой зависит от текущего `ModelProvider`, — писать её в плане вслепую значило бы дать код, который не скомпилируется. В каждом месте названо, по какому существующему тесту брать образец и что именно тест обязан проверять. Это единственные места без готового кода.

**Согласованность имён.** `ImageRef` (задача 1) используется в 3, 4, 7 с теми же полями. `MAX_IMAGES_IN_CONTEXT` объявлена в 3 и больше нигде не переопределяется. `_image_refs` объявлен в 4, вызывается там же дважды. Ключ `path` в блоке изображения вводится в задаче 2 и читается в задаче 4 — единственная пара, где данные пересекают границу задач.

**Риск, зафиксированный осознанно.** Задача 3 меняет сигнатуру `_to_openai_messages` и добавляет параметр конструктора провайдера. Это трогает все существующие вызовы провайдера; поэтому первым тестом задачи стоит проверка обратной совместимости — сообщение без изображений обязано рендериться ровно как раньше.
