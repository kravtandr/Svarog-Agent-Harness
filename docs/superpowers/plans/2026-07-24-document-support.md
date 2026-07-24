# Поддержка документов и изображений — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Внешние executor'ы (claude-code, opencode) читают PDF/DOCX/XLSX/PPTX/HTML/EPUB и изображения: конвертеры в docker-образах + MCP-инструменты `read_document`/`read_image` в мосте Svarog.

**Architecture:** Два слоя по спеке `docs/superpowers/specs/2026-07-24-document-support-design.md`: (1) apt-пакеты pandoc/poppler-utils/tesseract в оба sandbox-образа — bash-путь; (2) новые инструменты в существующем MCP-сервере моста (`BridgeControl._build_tools`), парсинг на хосте через опциональный `markitdown`, изображения — MCP image content blocks (требует расширить `ToolResult` полем `blocks`).

**Tech Stack:** Python 3.12, pydantic 2, pytest (asyncio_mode=auto), uv, ruff (line-length 100), mypy strict, markitdown (опционально), Docker (Debian slim образы).

## Global Constraints

- Python `>=3.12`; mypy strict; ruff line-length 100 (`uv run ruff check src tests`, `uv run mypy`).
- Комментарии и docstrings — на русском (стиль репозитория); комментарий пишется только ради ограничения, которое код не показывает.
- Тесты запускаются `uv run pytest …`; asyncio-тесты — обычные `async def`, режим auto.
- Версии npm/apt в Dockerfile'ах не пинятся (ADR-0016 §8: дрейф ловят тесты, не пиннинг).
- Опциональная зависимость: группа `docs = ["markitdown[pdf,docx,xlsx,xls,pptx,epub]>=0.1.2"]`; базовая установка работает без неё.
- Лимиты: вывод `read_document` — 40 000 символов (`truncate_text`); файл `read_image` — 5 MB.
- MCP image content block: `{"type": "image", "data": "<base64>", "mimeType": "image/png"}`.
- Коммиты — conventional commits со scope, как в истории репо (`feat(...)`, `docs(...)`).

---

### Task 1: Конвертеры документов в docker-образах

**Files:**
- Modify: `docker/agent-claude/Dockerfile:12-14`
- Modify: `docker/agent-opencode/Dockerfile:15-17`

**Interfaces:**
- Consumes: ничего (независимая задача).
- Produces: в обоих образах доступны бинари `pandoc`, `pdftotext`, `pdftoppm`, `tesseract` (+языки rus/eng) — на них ссылается hint из Task 6.

- [ ] **Step 1: agent-claude — добавить пакеты**

В `docker/agent-claude/Dockerfile` заменить блок apt-get (строки 12–14):

```dockerfile
# poppler-utils (pdftotext/pdftoppm), pandoc, tesseract (+rus/eng) — чтение
# документов и сканов агентом через bash (spec 2026-07-24): сеть sandbox'а
# изолирована, скачать конвертер в рантайме нельзя — только из образа.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 git ca-certificates \
        pandoc poppler-utils \
        tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 2: agent-opencode — добавить пакеты**

В `docker/agent-opencode/Dockerfile` заменить блок apt-get (строки 15–17):

```dockerfile
# poppler-utils (pdftotext/pdftoppm), pandoc, tesseract (+rus/eng) — чтение
# документов и сканов агентом через bash (spec 2026-07-24): сеть sandbox'а
# изолирована, скачать конвертер в рантайме нельзя — только из образа.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates ripgrep \
        pandoc poppler-utils \
        tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 3: smoke-проверка сборки (ручная, если docker доступен)**

Run: `docker build -t svarog-agent-claude-test docker/agent-claude && docker run --rm --entrypoint sh svarog-agent-claude-test -c "pandoc --version | head -1 && pdftotext -v 2>&1 | head -1 && tesseract --version 2>&1 | head -1"`
Expected: версии всех трёх утилит. Если docker недоступен в среде исполнения — пропустить шаг и отметить это в отчёте по задаче (CI-сборки образов в проекте нет, спека это фиксирует).

- [ ] **Step 4: Commit**

```bash
git add docker/agent-claude/Dockerfile docker/agent-opencode/Dockerfile
git commit -m "feat(docker): конвертеры документов и OCR в sandbox-образах"
```

---

### Task 2: `ToolResult.blocks` и content blocks в мосте

**Files:**
- Modify: `src/svarog_harness/tools/base.py:61-76` (класс `ToolResult`)
- Modify: `src/svarog_harness/runtime/bridge_control.py:190-241` (`handle_mcp` ветка `tools/call`, `_call_tool`)
- Test: `tests/test_bridge.py`

**Interfaces:**
- Consumes: текущие `ToolResult.success/.failure`, `BridgeControl.handle_mcp`.
- Produces: `ToolResult(blocks: list[dict[str, Any]] | None = None)`; `BridgeControl._call_tool(name, arguments) -> tuple[list[dict[str, Any]], bool]` — список MCP content blocks. Инструмент с `result.ok and result.blocks` отдаёт свои blocks вместо текстового; Task 3 опирается на это.

- [ ] **Step 1: Написать падающий тест**

В `tests/test_bridge.py` добавить (рядом с MCP-тестами; фикстура `_control` уже есть):

```python
class _BlocksArgs(BaseModel):
    pass


class _BlocksTool(Tool[_BlocksArgs]):
    name = "fake_blocks"
    description = "тестовый tool с image-блоком"
    risk_level = RiskLevel.LOW
    args_model = _BlocksArgs

    async def execute(self, args: _BlocksArgs) -> ToolResult:
        return ToolResult(
            ok=True,
            output="картинка",
            blocks=[{"type": "image", "data": "aGk=", "mimeType": "image/png"}],
        )


async def test_mcp_tool_result_blocks(tmp_path: Path) -> None:
    control = _control(tmp_path)
    control._tools["fake_blocks"] = _BlocksTool()
    reply = await control.handle_mcp(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "fake_blocks", "arguments": {}}}
    )
    content = reply["result"]["content"]
    assert content == [{"type": "image", "data": "aGk=", "mimeType": "image/png"}]
    assert reply["result"]["isError"] is False


async def test_mcp_tool_result_text_unchanged(tmp_path: Path) -> None:
    """Регресс: текстовые инструменты отдают одиночный text-блок как раньше."""
    mem = tmp_path / "memory"
    mem.mkdir()
    control = _control(tmp_path, memory_dir=mem)
    reply = await control.handle_mcp(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "read_memory", "arguments": {}}}
    )
    content = reply["result"]["content"]
    assert len(content) == 1 and content[0]["type"] == "text"
```

Импорты, которых может не хватать в шапке test_bridge.py: `from pydantic import BaseModel`, `from svarog_harness.tools.base import RiskLevel, Tool, ToolResult`.

- [ ] **Step 2: Убедиться, что тест падает**

Run: `uv run pytest tests/test_bridge.py::test_mcp_tool_result_blocks -v`
Expected: FAIL — `ToolResult` не принимает `blocks` (ValidationError) либо AttributeError.

- [ ] **Step 3: Реализация**

`src/svarog_harness/tools/base.py` — в класс `ToolResult` после поля `error`:

```python
    # MCP content blocks (image и т.п.): мост отдаёт их вместо текстового
    # блока. None — обычный текстовый результат (все существующие tools).
    blocks: list[dict[str, Any]] | None = None
```

(в шапке файла добавить `from typing import Any` — сейчас он уже импортирован, проверить.)

`src/svarog_harness/runtime/bridge_control.py` — ветка `tools/call` в `handle_mcp` (строки 190–202):

```python
            case "tools/call":
                params = payload.get("params") or {}
                name = str(params.get("name", ""))
                arguments = params.get("arguments")
                arguments = arguments if isinstance(arguments, dict) else {}
                content, is_error = await self._call_tool(name, arguments)
                return _rpc_result(msg_id, {"content": content, "isError": is_error})
```

`_call_tool` — новый тип возврата `tuple[list[dict[str, Any]], bool]`. Ветки `ask_user`/`request_approval` оборачивают свой текст: результат `_human_gate` (был `return await self._human_gate(...)`) превратить в:

```python
            text, is_error = await self._human_gate(
                ...без изменений аргументов...
            )
            return [{"type": "text", "text": text}], is_error
```

(обе ветки). Финал метода:

```python
        result = await tool.call(arguments)
        await self._flush_side_effects()
        text = redact(
            result.output if result.ok else (result.error or "ошибка"), self._secret_values
        )
        if self._on_notify is not None:
            self._on_notify("bridge.mcp", f"{name}: {'ok' if result.ok else 'ошибка'}")
        if result.ok and result.blocks:
            # Бинарные блоки redaction не проходят: источник ограничен workspace,
            # секреты в байтах картинок не живут (spec 2026-07-24).
            return result.blocks, False
        return [{"type": "text", "text": text}], not result.ok
```

Ветку `if tool is None` тоже обернуть: `return [{"type": "text", "text": f"неизвестный MCP-tool: {name}"}], True`.

- [ ] **Step 4: Прогнать тесты**

Run: `uv run pytest tests/test_bridge.py -v`
Expected: все PASS (новые и существующие).

- [ ] **Step 5: Линт/типы и коммит**

Run: `uv run ruff check src tests && uv run mypy`
Expected: без ошибок.

```bash
git add src/svarog_harness/tools/base.py src/svarog_harness/runtime/bridge_control.py tests/test_bridge.py
git commit -m "feat(bridge): MCP content blocks в результатах инструментов"
```

---

### Task 3: `read_image` — изображение как MCP image block

**Files:**
- Create: `src/svarog_harness/tools/document_tools.py`
- Test: `tests/test_document_tools.py` (создать)

**Interfaces:**
- Consumes: `ToolResult.blocks` из Task 2; `Tool`, `RiskLevel`, `ToolError` из `tools/base.py`.
- Produces: `resolve_workspace_path(workspace: Path, rel: str) -> Path` (fail-closed); `ReadImageTool(workspace_dir: Path)` с `name="read_image"`. Task 4 дописывает в этот же модуль `ReadDocumentTool`; Task 5 регистрирует оба в мосте.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_document_tools.py`:

```python
"""Тесты MCP-инструментов документов/изображений (spec 2026-07-24)."""

import base64
from pathlib import Path

import pytest

from svarog_harness.tools.base import ToolError
from svarog_harness.tools.document_tools import (
    ReadImageTool,
    resolve_workspace_path,
)

# Валидный однопиксельный PNG.
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
)


def test_resolve_inside_workspace(tmp_path: Path) -> None:
    (tmp_path / "a.png").write_bytes(_PNG_1PX)
    assert resolve_workspace_path(tmp_path, "a.png") == (tmp_path / "a.png").resolve()


def test_resolve_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(ToolError):
        resolve_workspace_path(tmp_path, "../etc/passwd")


def test_resolve_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(_PNG_1PX)
    (tmp_path / "link.png").symlink_to(outside)
    with pytest.raises(ToolError):
        resolve_workspace_path(tmp_path, "link.png")


def test_resolve_rejects_missing(tmp_path: Path) -> None:
    with pytest.raises(ToolError):
        resolve_workspace_path(tmp_path, "нет.png")


async def test_read_image_returns_block(tmp_path: Path) -> None:
    (tmp_path / "pic.png").write_bytes(_PNG_1PX)
    result = await ReadImageTool(tmp_path).call({"path": "pic.png"})
    assert result.ok
    assert result.blocks is not None and len(result.blocks) == 1
    block = result.blocks[0]
    assert block["type"] == "image"
    assert block["mimeType"] == "image/png"
    assert base64.b64decode(block["data"]) == _PNG_1PX


async def test_read_image_rejects_unsupported_format(tmp_path: Path) -> None:
    (tmp_path / "doc.bmp").write_bytes(b"BM")
    result = await ReadImageTool(tmp_path).call({"path": "doc.bmp"})
    assert not result.ok
    assert ".bmp" in (result.error or "")


async def test_read_image_rejects_oversize(tmp_path: Path) -> None:
    (tmp_path / "big.png").write_bytes(b"\x00" * (5 * 1024 * 1024 + 1))
    result = await ReadImageTool(tmp_path).call({"path": "big.png"})
    assert not result.ok
    assert "лимит" in (result.error or "")
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_document_tools.py -v`
Expected: FAIL на импорте — модуля `document_tools` нет.

- [ ] **Step 3: Реализация**

Создать `src/svarog_harness/tools/document_tools.py`:

```python
"""MCP-инструменты документов и изображений workspace (spec 2026-07-24).

`read_image` отдаёт картинку MCP image content block'ом — vision для агентов
без нативного чтения изображений (opencode); `read_document` (Task 4)
конвертирует офисные форматы в Markdown через опциональный markitdown.
Оба читают ТОЛЬКО из workspace: побег пути — fail-closed ToolError.
"""

import base64
from pathlib import Path

from pydantic import BaseModel, Field

from svarog_harness.tools.base import RiskLevel, Tool, ToolError, ToolResult

# Лимит Anthropic API на изображение — ~5 MB; больший файл модель не примет.
_IMAGE_LIMIT_BYTES = 5 * 1024 * 1024
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def resolve_workspace_path(workspace: Path, rel: str) -> Path:
    """Путь строго внутри workspace; `..` и symlink-побеги — fail-closed."""
    if not rel.strip():
        raise ToolError("путь пуст")
    root = workspace.resolve()
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolError(f"путь '{rel}' выходит за пределы workspace")
    if not candidate.is_file():
        raise ToolError(f"файла '{rel}' нет в workspace")
    return candidate


class ReadImageArgs(BaseModel):
    path: str = Field(description="Путь к изображению относительно workspace (png/jpg/gif/webp)")


class ReadImageTool(Tool[ReadImageArgs]):
    name = "read_image"
    action_type = "file.read"
    description = (
        "Показать модели изображение из workspace (vision): PNG/JPEG/GIF/WebP "
        "до 5 MB. Скан PDF сначала переведи в PNG постранично: "
        "`pdftoppm -png -r 150 файл.pdf стр`"
    )
    risk_level = RiskLevel.LOW
    args_model = ReadImageArgs

    def __init__(self, workspace_dir: Path) -> None:
        self._workspace = workspace_dir

    def is_read_only(self, args: ReadImageArgs) -> bool:
        return True

    async def execute(self, args: ReadImageArgs) -> ToolResult:
        path = resolve_workspace_path(self._workspace, args.path)
        mime = _IMAGE_MIME.get(path.suffix.lower())
        if mime is None:
            supported = ", ".join(sorted(_IMAGE_MIME))
            return ToolResult.failure(
                f"формат '{path.suffix}' не поддержан; доступны: {supported}"
            )
        size = path.stat().st_size
        if size > _IMAGE_LIMIT_BYTES:
            return ToolResult.failure(
                f"файл {size} байт превышает лимит {_IMAGE_LIMIT_BYTES}; "
                "уменьшите разрешение (например `pdftoppm -r 150`)"
            )
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return ToolResult(
            ok=True,
            output=f"изображение {args.path} ({mime}, {size} байт)",
            blocks=[{"type": "image", "data": data, "mimeType": mime}],
        )
```

- [ ] **Step 4: Прогнать тесты**

Run: `uv run pytest tests/test_document_tools.py -v`
Expected: все PASS.

- [ ] **Step 5: Линт/типы и коммит**

Run: `uv run ruff check src tests && uv run mypy`
Expected: без ошибок.

```bash
git add src/svarog_harness/tools/document_tools.py tests/test_document_tools.py
git commit -m "feat(tools): read_image — изображение как MCP image block"
```

---

### Task 4: `read_document` через markitdown + группа зависимостей `docs`

**Files:**
- Modify: `pyproject.toml:37-48` (optional-dependencies), `pyproject.toml:56-67` (dev-группа), `pyproject.toml:104-113` (mypy override)
- Modify: `src/svarog_harness/tools/document_tools.py`
- Test: `tests/test_document_tools.py`

**Interfaces:**
- Consumes: `resolve_workspace_path`, `ToolResult`, `truncate_text` из Task 3 / base.
- Produces: `document_tools_available() -> bool`; `ReadDocumentTool(workspace_dir: Path)` с `name="read_document"`, аргументы `{path, offset, limit}`. Task 5 регистрирует по флагу `document_tools_available()`; Task 7 использует его же в doctor.

- [ ] **Step 1: Зависимости**

`pyproject.toml`, в `[project.optional-dependencies]` после группы `mcp`:

```toml
# Конвертация документов для MCP-tool read_document (spec 2026-07-24).
# Ставится как `svarog-harness[docs]`; без неё read_document отсутствует.
docs = [
    "markitdown[pdf,docx,xlsx,xls,pptx,epub]>=0.1.2",
]
```

В `[dependency-groups] dev` добавить строку:

```toml
    "markitdown[pdf,docx,xlsx,xls,pptx,epub]>=0.1.2",
```

После mypy-секции `[[tool.mypy.overrides]]` для migrations добавить:

```toml
[[tool.mypy.overrides]]
# У markitdown нет type stubs.
module = "markitdown.*"
ignore_missing_imports = true
```

Run: `uv sync`
Expected: markitdown установлен, `uv.lock` обновлён.

- [ ] **Step 2: Написать падающие тесты**

Дописать в `tests/test_document_tools.py` (шапка: добавить `from svarog_harness.tools.document_tools import ReadDocumentTool, document_tools_available`):

```python
def test_document_tools_available() -> None:
    # dev-группа ставит markitdown — в тестовой среде инструмент включён.
    assert document_tools_available()


async def test_read_document_html(tmp_path: Path) -> None:
    (tmp_path / "doc.html").write_text(
        "<h1>Отчёт</h1><p>первый абзац</p><p>второй абзац</p>", encoding="utf-8"
    )
    result = await ReadDocumentTool(tmp_path).call({"path": "doc.html"})
    assert result.ok
    assert "Отчёт" in result.output
    assert "второй абзац" in result.output


async def test_read_document_xlsx(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "город"
    ws["A2"] = "Москва"
    wb.save(tmp_path / "data.xlsx")
    result = await ReadDocumentTool(tmp_path).call({"path": "data.xlsx"})
    assert result.ok
    assert "Москва" in result.output


async def test_read_document_offset_limit(tmp_path: Path) -> None:
    (tmp_path / "doc.html").write_text("<p>один</p><p>два</p><p>три</p>", encoding="utf-8")
    full = await ReadDocumentTool(tmp_path).call({"path": "doc.html"})
    total_lines = len(full.output.split("\n\n", 1)[1].splitlines())
    windowed = await ReadDocumentTool(tmp_path).call(
        {"path": "doc.html", "offset": 1, "limit": 1}
    )
    assert windowed.ok
    body = windowed.output.split("\n\n", 1)[1]
    assert len(body.splitlines()) == 1
    assert total_lines >= 2


async def test_read_document_unsupported_format(tmp_path: Path) -> None:
    (tmp_path / "prog.xyz").write_text("data", encoding="utf-8")
    result = await ReadDocumentTool(tmp_path).call({"path": "prog.xyz"})
    assert not result.ok
    assert "pandoc" in (result.error or "")  # подсказка про bash-конвертеры


async def test_read_document_escape_rejected(tmp_path: Path) -> None:
    result = await ReadDocumentTool(tmp_path).call({"path": "../secret.pdf"})
    assert not result.ok
```

- [ ] **Step 3: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_document_tools.py -v`
Expected: новые тесты FAIL (ImportError: `ReadDocumentTool`), тесты Task 3 — PASS.

- [ ] **Step 4: Реализация**

Дописать в `src/svarog_harness/tools/document_tools.py` (шапка: добавить `import asyncio`, `import importlib.util`, `from svarog_harness.tools.base import truncate_text`):

```python
# Потолок одного ответа — как у read_svarog_docs (§6.3 backpressure).
_OUTPUT_LIMIT = 40_000
# Форматы, которые markitdown конвертирует в Markdown надёжно.
_DOCUMENT_SUFFIXES = (
    ".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".html", ".htm", ".epub", ".csv",
)


def document_tools_available() -> bool:
    """Установлена ли опциональная группа `docs` (markitdown)."""
    return importlib.util.find_spec("markitdown") is not None


class ReadDocumentArgs(BaseModel):
    path: str = Field(
        description="Путь к документу относительно workspace: PDF/DOCX/XLSX/PPTX/HTML/EPUB/CSV"
    )
    offset: int = Field(
        default=0, ge=0, description="С какой строки Markdown-результата начать"
    )
    limit: int | None = Field(
        default=None, ge=1, description="Сколько строк вернуть; пусто — до конца"
    )


class ReadDocumentTool(Tool[ReadDocumentArgs]):
    name = "read_document"
    action_type = "file.read"
    description = (
        "Прочитать документ из workspace как Markdown: PDF, DOCX, XLSX, PPTX, "
        "HTML, EPUB, CSV. Для длинных документов листай offset/limit. "
        "Сканы без текстового слоя конвертер не прочтёт — используй tesseract "
        "или pdftoppm + read_image"
    )
    risk_level = RiskLevel.LOW
    args_model = ReadDocumentArgs
    # Крупный PDF конвертируется дольше дефолтных 60с.
    timeout_sec = 120.0

    def __init__(self, workspace_dir: Path) -> None:
        self._workspace = workspace_dir

    def is_read_only(self, args: ReadDocumentArgs) -> bool:
        return True

    async def execute(self, args: ReadDocumentArgs) -> ToolResult:
        path = resolve_workspace_path(self._workspace, args.path)
        if path.suffix.lower() not in _DOCUMENT_SUFFIXES:
            supported = ", ".join(_DOCUMENT_SUFFIXES)
            return ToolResult.failure(
                f"формат '{path.suffix}' не поддержан ({supported}); "
                "другие форматы конвертируй в sandbox через pandoc/pdftotext"
            )
        from markitdown import MarkItDown

        def _convert() -> str:
            return str(MarkItDown(enable_plugins=False).convert(str(path)).text_content)

        try:
            text = await asyncio.to_thread(_convert)
        except Exception as exc:  # noqa: BLE001 — парсеры кидают разнотипное
            return ToolResult.failure(f"не удалось сконвертировать '{args.path}': {exc}")
        lines = text.splitlines()
        window = lines[args.offset :]
        if args.limit is not None:
            window = window[: args.limit]
        header = (
            f"# {args.path} (строки {args.offset}–{args.offset + len(window)} "
            f"из {len(lines)})"
        )
        return ToolResult.success(truncate_text(header + "\n\n" + "\n".join(window), _OUTPUT_LIMIT))
```

Примечание: `except Exception` намеренно широкий — markitdown пробрасывает ошибки вложенных парсеров разных типов; мост не должен падать (спека, раздел «Обработка ошибок»). Если ruff всё же ругается (правило BLE не включено в конфиг — скорее всего нет), убрать noqa.

- [ ] **Step 5: Прогнать тесты**

Run: `uv run pytest tests/test_document_tools.py -v`
Expected: все PASS.

- [ ] **Step 6: Линт/типы и коммит**

Run: `uv run ruff check src tests && uv run mypy`
Expected: без ошибок.

```bash
git add pyproject.toml uv.lock src/svarog_harness/tools/document_tools.py tests/test_document_tools.py
git commit -m "feat(tools): read_document — офисные форматы в Markdown через markitdown"
```

---

### Task 5: Регистрация в мосте + workspace_dir

**Files:**
- Modify: `src/svarog_harness/runtime/bridge_control.py:89-153` (конструктор, `_build_tools`)
- Modify: `src/svarog_harness/runtime/run_assembly.py:366-378` (вызов `BridgeControl(...)`)
- Test: `tests/test_bridge.py`

**Interfaces:**
- Consumes: `ReadImageTool`, `ReadDocumentTool`, `document_tools_available` из Task 3/4.
- Produces: `BridgeControl(..., workspace_dir: Path | None = None)`; в `tools/list` появляются `read_image` (всегда при workspace_dir) и `read_document` (при установленном markitdown). Task 6 ссылается на имена `read_image`/`read_document` в hint'ах.

- [ ] **Step 1: Написать падающий тест**

В `tests/test_bridge.py`: в хелпер `_control` (строка ~355) добавить параметр и прокинуть его:

```python
    workspace_dir: Path | None = None,
```

и в конструкторе `BridgeControl(...)` внутри `_control`:

```python
        workspace_dir=workspace_dir,
```

Новый тест:

```python
async def test_mcp_document_tools_registered(tmp_path: Path) -> None:
    control = _control(tmp_path, workspace_dir=tmp_path)
    listed = await control.handle_mcp({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert "read_image" in names
    assert "read_document" in names  # dev-среда ставит markitdown


async def test_mcp_document_tools_absent_without_workspace(tmp_path: Path) -> None:
    control = _control(tmp_path)
    listed = await control.handle_mcp({"jsonrpc": "2.0", "id": 8, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert "read_image" not in names and "read_document" not in names


async def test_mcp_read_image_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "pic.png").write_bytes(_PNG_1PX)
    control = _control(tmp_path, workspace_dir=tmp_path)
    reply = await control.handle_mcp(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
         "params": {"name": "read_image", "arguments": {"path": "pic.png"}}}
    )
    content = reply["result"]["content"]
    assert content[0]["type"] == "image" and content[0]["mimeType"] == "image/png"
```

В шапку test_bridge.py добавить константу `_PNG_1PX` (та же, что в tests/test_document_tools.py) либо импортировать её оттуда: `from tests.test_document_tools import _PNG_1PX` — если конвенция репо не позволяет кросс-импорт тестов, продублировать константу.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_bridge.py -k document_tools -v`
Expected: FAIL — `BridgeControl.__init__` не знает `workspace_dir`.

- [ ] **Step 3: Реализация**

`src/svarog_harness/runtime/bridge_control.py`:

- импорт: `from svarog_harness.tools.document_tools import ReadDocumentTool, ReadImageTool, document_tools_available`;
- конструктор: параметр `workspace_dir: Path | None = None` (после `memory_dir`), сохранить `self._workspace_dir = workspace_dir`;
- в `_build_tools()` перед `return tools`:

```python
        # Документы/изображения workspace (spec 2026-07-24): read_image без
        # зависимостей; read_document — только при установленном markitdown
        # (группа `docs`), отсутствие — фича молча выключена, doctor подскажет.
        if self._workspace_dir is not None:
            tools["read_image"] = ReadImageTool(self._workspace_dir)
            if document_tools_available():
                tools["read_document"] = ReadDocumentTool(self._workspace_dir)
```

`src/svarog_harness/runtime/run_assembly.py`, вызов `BridgeControl(...)` (строка 366) — добавить аргумент:

```python
            workspace_dir=workspace,
```

- [ ] **Step 4: Прогнать тесты**

Run: `uv run pytest tests/test_bridge.py tests/test_document_tools.py -v`
Expected: все PASS.

- [ ] **Step 5: Линт/типы и коммит**

Run: `uv run ruff check src tests && uv run mypy`
Expected: без ошибок.

```bash
git add src/svarog_harness/runtime/bridge_control.py src/svarog_harness/runtime/run_assembly.py tests/test_bridge.py
git commit -m "feat(bridge): регистрация read_document/read_image в MCP-сервере моста"
```

---

### Task 6: Hint в контекст-файлах агентов

**Files:**
- Modify: `src/svarog_harness/tools/document_tools.py` (функция hint)
- Modify: `src/svarog_harness/runtime/executor.py:173-180` (протокол `context_files`)
- Modify: `src/svarog_harness/runtime/agents/claude_code.py:103-127`
- Modify: `src/svarog_harness/runtime/agents/opencode.py:68-95`
- Modify: `src/svarog_harness/runtime/agents/codex.py:75-83` (сигнатура, игнорирует)
- Modify: `src/svarog_harness/runtime/agent_infra.py:172` (вызов `context_files`)
- Test: `tests/test_agent_adapters.py`

**Interfaces:**
- Consumes: `document_tools_available()` из Task 4; имена инструментов из Task 5.
- Produces: `document_tools_hint(read_document: str, read_image: str) -> str`; протокол `context_files(self, memory, skill_cards, self_docs=False, doc_tools=False)` — все три адаптера принимают новый параметр.

- [ ] **Step 1: Написать падающие тесты**

В `tests/test_agent_adapters.py`:

```python
def test_claude_context_files_doc_tools() -> None:
    files = ClaudeCodeAdapter().context_files("", "", doc_tools=True)
    body = files["CLAUDE.md"]
    assert "mcp__svarog__read_image" in body
    assert "pdftotext" in body
    # dev-среда ставит markitdown — hint упоминает и read_document.
    assert "mcp__svarog__read_document" in body


def test_claude_context_files_no_doc_tools() -> None:
    files = ClaudeCodeAdapter().context_files("", "")
    assert "read_image" not in files.get("CLAUDE.md", "")


def test_opencode_context_files_doc_tools() -> None:
    files = OpencodeAdapter().context_files("", "", doc_tools=True)
    body = files[".config/opencode/AGENTS.md"]
    assert "svarog_read_image" in body
    assert "svarog_read_document" in body
```

(Импорты `ClaudeCodeAdapter`/`OpencodeAdapter` в этом файле уже есть — проверить шапку.)

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_agent_adapters.py -k doc_tools -v`
Expected: FAIL — `context_files` не принимает `doc_tools`.

- [ ] **Step 3: Реализация**

В `src/svarog_harness/tools/document_tools.py` добавить:

```python
def document_tools_hint(read_document: str, read_image: str) -> str:
    """Блок контекст-файла агента: как читать документы и изображения.

    Имена инструментов у адаптеров разные (mcp__svarog__* / svarog_*) —
    подставляются параметрами, ср. ask_user_guide.
    """
    parts = [
        "# Документы и изображения",
        "В sandbox есть конвертеры: `pdftotext` (текстовый слой PDF), `pandoc` "
        "(DOCX/HTML/EPUB/RTF → Markdown), `tesseract -l rus+eng` (OCR сканов), "
        "`pdftoppm -png -r 150` (PDF → PNG постранично).",
        f"Показать модели картинку из workspace — MCP-tool `{read_image}` "
        "(PNG/JPEG/GIF/WebP до 5 MB).",
    ]
    if document_tools_available():
        parts.append(
            f"XLSX/PPTX и любой офисный формат целиком — MCP-tool `{read_document}` "
            "(результат — Markdown, длинные документы листай offset/limit)."
        )
    parts.append(
        "Скан без текстового слоя: `tesseract` (дёшево) либо `pdftoppm` → "
        f"`{read_image}` постранично (vision, качество выше, дороже по токенам)."
    )
    return "\n\n".join(parts)
```

`src/svarog_harness/runtime/executor.py` — сигнатура протокола:

```python
    def context_files(
        self, memory: str, skill_cards: str, self_docs: bool = False, doc_tools: bool = False
    ) -> dict[str, str]:
        """Файлы контекста агента (ADR-0016 §4): относительный путь внутри
        state_dir → содержимое (CLAUDE.md / AGENTS.md); пусто — контекст
        не передаётся. self_docs — доступен ли reverse-tool `read_svarog_docs`;
        doc_tools — зарегистрированы ли инструменты документов/изображений
        (spec 2026-07-24). Адаптер называет tools в своём неймспейсе;
        при mcp=False оба флага игнорируются."""
        ...
```

`claude_code.py` — сигнатура `context_files(..., self_docs: bool = False, doc_tools: bool = False)`; после блока `if self_docs:` добавить:

```python
        if doc_tools:
            sections.append(
                document_tools_hint("mcp__svarog__read_document", "mcp__svarog__read_image")
            )
```

импорт: `from svarog_harness.tools.document_tools import document_tools_hint`.

`opencode.py` — аналогично: сигнатура + `document_tools_hint("svarog_read_document", "svarog_read_image")` + импорт.

`codex.py` — только сигнатура `(..., self_docs: bool = False, doc_tools: bool = False)`; в docstring дописать «doc_tools игнорируется по той же причине» (mcp=False).

`agent_infra.py:172` — вызов:

```python
        state_files = dict(
            self._adapter.context_files(
                memory, skill_cards, self_docs, doc_tools=self._adapter.capabilities().mcp
            )
        )
```

- [ ] **Step 4: Прогнать тесты**

Run: `uv run pytest tests/test_agent_adapters.py tests/test_external_executor.py -v`
Expected: все PASS (второй файл — регресс инфраструктуры запуска).

- [ ] **Step 5: Линт/типы и коммит**

Run: `uv run ruff check src tests && uv run mypy`
Expected: без ошибок.

```bash
git add src/svarog_harness/tools/document_tools.py src/svarog_harness/runtime/executor.py \
    src/svarog_harness/runtime/agents/claude_code.py src/svarog_harness/runtime/agents/opencode.py \
    src/svarog_harness/runtime/agents/codex.py src/svarog_harness/runtime/agent_infra.py \
    tests/test_agent_adapters.py
git commit -m "feat(agents): hint про документы/изображения в контекст-файлах"
```

---

### Task 7: Doctor-подсказка про markitdown

**Files:**
- Modify: `src/svarog_harness/cli/doctor.py` (`collect_checks` + новая проверка)
- Test: `tests/test_cli_doctor.py`

**Interfaces:**
- Consumes: `document_tools_available()` из Task 4; `DoctorCheck` из doctor.py.
- Produces: проверка с именем `document-tools` в выводе `svarog doctor`.

- [ ] **Step 1: Написать падающий тест**

В `tests/test_cli_doctor.py`:

```python
def test_check_document_tools_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from svarog_harness.cli import doctor

    monkeypatch.setattr(doctor, "document_tools_available", lambda: True)
    check = doctor._check_document_tools()
    assert check.status == "ok"


def test_check_document_tools_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    from svarog_harness.cli import doctor

    monkeypatch.setattr(doctor, "document_tools_available", lambda: False)
    check = doctor._check_document_tools()
    assert check.status == "warn"
    assert "svarog-harness[docs]" in check.hint
```

(если `import pytest` в шапке файла отсутствует — добавить.)

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_cli_doctor.py -k document_tools -v`
Expected: FAIL — `_check_document_tools` не существует.

- [ ] **Step 3: Реализация**

`src/svarog_harness/cli/doctor.py` — импорт `from svarog_harness.tools.document_tools import document_tools_available`, новая функция:

```python
def _check_document_tools() -> DoctorCheck:
    if document_tools_available():
        return DoctorCheck(
            "document-tools", "ok", "markitdown установлен — MCP-tool read_document доступен"
        )
    return DoctorCheck(
        "document-tools",
        "warn",
        "markitdown не установлен — read_document (PDF/DOCX/XLSX/PPTX через мост) выключен",
        hint="установить: pip install 'svarog-harness[docs]'",
    )
```

В `collect_checks` (строка 40) добавить `checks.append(_check_document_tools())` рядом с другими независимыми от конфига проверками.

- [ ] **Step 4: Прогнать тесты**

Run: `uv run pytest tests/test_cli_doctor.py -v`
Expected: все PASS.

- [ ] **Step 5: Линт/типы и коммит**

Run: `uv run ruff check src tests && uv run mypy`
Expected: без ошибок.

```bash
git add src/svarog_harness/cli/doctor.py tests/test_cli_doctor.py
git commit -m "feat(doctor): проверка группы docs (markitdown) для read_document"
```

---

### Task 8: Документация README + финальный прогон

**Files:**
- Modify: `README.md` (раздел установки / возможностей — найти по месту)
- Спека: `docs/superpowers/specs/2026-07-24-document-support-design.md` — обновить «Статус: реализовано»

**Interfaces:**
- Consumes: всё предыдущее.
- Produces: пользовательская документация.

- [ ] **Step 1: README**

В раздел установки добавить упоминание опциональной группы (рядом с `[server]`/`[mcp]`, найти по grep `svarog-harness[`):

```markdown
- `pip install 'svarog-harness[docs]'` — MCP-инструмент `read_document`: чтение
  PDF/DOCX/XLSX/PPTX/HTML/EPUB из workspace как Markdown (через markitdown).
```

В описание sandbox-образов (grep `agent-claude` по README) добавить одно предложение: образы включают `pandoc`, `poppler-utils` (pdftotext/pdftoppm) и `tesseract` (rus/eng) — агент конвертирует документы и OCR'ит сканы через bash; изображения модель видит через нативный `Read` (claude) или MCP-tool `read_image` (все агенты с MCP).

- [ ] **Step 2: Статус спеки**

В `docs/superpowers/specs/2026-07-24-document-support-design.md` заменить `**Статус:** одобрено, ждёт реализации` на `**Статус:** реализовано`.

- [ ] **Step 3: Полный прогон**

Run: `uv run pytest && uv run ruff check src tests && uv run mypy`
Expected: всё зелёное.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/superpowers/specs/2026-07-24-document-support-design.md
git commit -m "docs: поддержка документов и изображений — README и статус спеки"
```
