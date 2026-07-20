"""Tests for the asynchronous chat setting picker."""

import pytest

from svarog_harness.cli import chat_picker


async def test_pick_option_uses_running_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dialog must not call Application.run() from InlineChat's loop."""

    called = False

    class FakeDialog:
        async def run_async(self) -> str:
            nonlocal called
            called = True
            return "external"

    monkeypatch.setattr(chat_picker, "radiolist_dialog", lambda **_kwargs: FakeDialog())

    choice = await chat_picker.pick_option(
        "executor", [("native", "native"), ("external", "external")], default="native"
    )

    assert choice == "external"
    assert called
