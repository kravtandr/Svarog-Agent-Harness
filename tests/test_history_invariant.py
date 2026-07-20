"""Инвариант истории перед вызовом модели (блок A §1)."""

import pytest

from svarog_harness.llm.provider import ChatMessage, ToolCallRequest
from svarog_harness.runtime.history_invariant import (
    HistoryInvariantError,
    assert_history_valid,
)


def _system() -> ChatMessage:
    return ChatMessage(role="system", content="ты агент")


def _call(call_id: str) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name="read_file", arguments_json='{"path": "a.txt"}')


def test_valid_history_passes() -> None:
    messages = [
        _system(),
        ChatMessage(role="user", content="читай"),
        ChatMessage(role="assistant", tool_calls=(_call("c1"),)),
        ChatMessage(role="tool", content="результат", tool_call_id="c1"),
    ]
    assert_history_valid(messages)


def test_history_without_system_first_fails() -> None:
    with pytest.raises(HistoryInvariantError, match="system"):
        assert_history_valid([ChatMessage(role="user", content="читай")])


def test_tool_call_without_result_fails() -> None:
    messages = [
        _system(),
        ChatMessage(role="assistant", tool_calls=(_call("c1"),)),
    ]
    with pytest.raises(HistoryInvariantError, match="c1"):
        assert_history_valid(messages)


def test_tool_result_without_call_fails() -> None:
    messages = [
        _system(),
        ChatMessage(role="tool", content="результат", tool_call_id="c9"),
    ]
    with pytest.raises(HistoryInvariantError, match="c9"):
        assert_history_valid(messages)


def test_duplicate_tool_result_fails() -> None:
    messages = [
        _system(),
        ChatMessage(role="assistant", tool_calls=(_call("c1"),)),
        ChatMessage(role="tool", content="раз", tool_call_id="c1"),
        ChatMessage(role="tool", content="два", tool_call_id="c1"),
    ]
    with pytest.raises(HistoryInvariantError, match="c1"):
        assert_history_valid(messages)


def test_empty_tool_call_name_fails() -> None:
    messages = [
        _system(),
        ChatMessage(
            role="assistant",
            tool_calls=(ToolCallRequest(id="c1", name="", arguments_json="{}"),),
        ),
        ChatMessage(role="tool", content="результат", tool_call_id="c1"),
    ]
    with pytest.raises(HistoryInvariantError, match="пустое имя"):
        assert_history_valid(messages)


def test_empty_history_fails() -> None:
    with pytest.raises(HistoryInvariantError, match="пустая"):
        assert_history_valid([])


def test_reused_call_id_across_turns_passes() -> None:
    """Небрежный сервер может переиспользовать tool_call_id между ходами;
    пока пары сбалансированы, история легальна."""
    messages = [
        _system(),
        ChatMessage(role="assistant", tool_calls=(_call("c"),)),
        ChatMessage(role="tool", content="раз", tool_call_id="c"),
        ChatMessage(role="assistant", tool_calls=(_call("c"),)),
        ChatMessage(role="tool", content="два", tool_call_id="c"),
    ]
    assert_history_valid(messages)
