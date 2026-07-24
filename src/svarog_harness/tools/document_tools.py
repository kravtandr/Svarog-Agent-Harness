"""MCP-инструменты документов и изображений workspace (spec 2026-07-24).

`read_image` отдаёт картинку MCP image content block'ом — vision для агентов
без нативного чтения изображений (opencode); `read_document` конвертирует
офисные форматы в Markdown через опциональный markitdown (группа `docs`).
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
