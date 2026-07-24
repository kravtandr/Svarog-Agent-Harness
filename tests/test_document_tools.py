"""Тесты MCP-инструментов документов/изображений (spec 2026-07-24)."""

import base64
from pathlib import Path

import pytest

from svarog_harness.tools.base import ToolError
from svarog_harness.tools.document_tools import (
    ReadDocumentTool,
    ReadImageTool,
    document_tools_available,
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
    full_lines = full.output.split("\n\n", 1)[1].splitlines()
    assert len(full_lines) >= 2
    windowed = await ReadDocumentTool(tmp_path).call(
        {"path": "doc.html", "offset": 1, "limit": 1}
    )
    assert windowed.ok
    # Окно — ровно срез строк полного результата (offset=1, одна строка).
    assert windowed.output.split("\n\n", 1)[1] == full_lines[1]


async def test_read_document_unsupported_format(tmp_path: Path) -> None:
    (tmp_path / "prog.xyz").write_text("data", encoding="utf-8")
    result = await ReadDocumentTool(tmp_path).call({"path": "prog.xyz"})
    assert not result.ok
    assert "pandoc" in (result.error or "")  # подсказка про bash-конвертеры


async def test_read_document_escape_rejected(tmp_path: Path) -> None:
    result = await ReadDocumentTool(tmp_path).call({"path": "../secret.pdf"})
    assert not result.ok
