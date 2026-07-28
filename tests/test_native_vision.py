"""Изображения в нативном цикле: ссылки, рендер, лимит (план 2026-07-28)."""

import pytest

from svarog_harness.llm.provider import ChatMessage, ImageRef
from svarog_harness.runtime.checkpoint import _message_from_dict, _message_to_dict
from svarog_harness.tools.document_tools import ReadImageArgs, ReadImageTool


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
