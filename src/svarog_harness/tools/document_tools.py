"""MCP-инструменты документов и изображений workspace (spec 2026-07-24).

`read_image` отдаёт картинку MCP image content block'ом — vision для агентов
без нативного чтения изображений (opencode); `read_document` конвертирует
офисные форматы в Markdown через опциональный markitdown (группа `docs`).
Оба читают ТОЛЬКО из workspace: побег пути — fail-closed ToolError.
"""

import asyncio
import base64
import importlib.util
from pathlib import Path

from pydantic import BaseModel, Field

from svarog_harness.tools.base import RiskLevel, Tool, ToolError, ToolResult, truncate_text

# Потолок одного ответа — как у read_svarog_docs (§6.3 backpressure).
_OUTPUT_LIMIT = 40_000
# Форматы, которые markitdown конвертирует в Markdown надёжно.
_DOCUMENT_SUFFIXES = (
    ".pdf",
    ".docx",
    ".xlsx",
    ".xls",
    ".pptx",
    ".html",
    ".htm",
    ".epub",
    ".csv",
)
# Лимит Anthropic API на изображение — ~5 MB; больший файл модель не примет.
_IMAGE_LIMIT_BYTES = 5 * 1024 * 1024
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def document_tools_available() -> bool:
    """Установлена ли опциональная группа `docs` (markitdown)."""
    return importlib.util.find_spec("markitdown") is not None


def document_tools_hint(read_document: str, read_image: str) -> str:
    """Блок контекст-файла агента: как читать документы и изображения.

    Имена tools у адаптеров разные (mcp__svarog__* / svarog_*) — подставляются
    параметрами, ср. ask_user_guide. Упоминание read_document — только при
    установленном markitdown: иначе указатель был бы ложью.
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


# Точка монтирования workspace в sandbox-контейнере (sandbox/docker.py).
_CONTAINER_WORKSPACE = "/workspace"


def resolve_workspace_path(workspace: Path, rel: str) -> Path:
    """Путь строго внутри workspace; `..` и symlink-побеги — fail-closed.

    Абсолютный контейнерный путь `/workspace/…` нормализуется в относительный:
    агент живёт в контейнере, где workspace примонтирован туда, и передаёт
    пути своими глазами (прогон S28).
    """
    if not rel.strip():
        raise ToolError("путь пуст")
    if rel == _CONTAINER_WORKSPACE or rel.startswith(_CONTAINER_WORKSPACE + "/"):
        rel = rel[len(_CONTAINER_WORKSPACE) + 1 :]
        if not rel:
            raise ToolError("путь пуст")
    root = workspace.resolve()
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolError(f"путь '{rel}' выходит за пределы workspace")
    if not candidate.is_file():
        raise ToolError(f"файла '{rel}' нет в workspace")
    return candidate


class ReadDocumentArgs(BaseModel):
    path: str = Field(
        description="Путь к документу относительно workspace: PDF/DOCX/XLSX/PPTX/HTML/EPUB/CSV"
    )
    offset: int = Field(default=0, ge=0, description="С какой строки Markdown-результата начать")
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
        except Exception as exc:  # парсеры кидают разнотипное; мост не падает
            return ToolResult.failure(f"не удалось сконвертировать '{args.path}': {exc}")
        lines = text.splitlines()
        window = lines[args.offset :]
        if args.limit is not None:
            window = window[: args.limit]
        header = f"# {args.path} (строки {args.offset}–{args.offset + len(window)} из {len(lines)})"
        return ToolResult.success(truncate_text(header + "\n\n" + "\n".join(window), _OUTPUT_LIMIT))


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
            return ToolResult.failure(f"формат '{path.suffix}' не поддержан; доступны: {supported}")
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
            # path — для нативного цикла: по нему строится ImageRef, который
            # переживает checkpoint вместо base64. Мост снимает ключ перед
            # отдачей блока в MCP.
            blocks=[{"type": "image", "data": data, "mimeType": mime, "path": args.path}],
        )
