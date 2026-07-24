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
