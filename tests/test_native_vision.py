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
