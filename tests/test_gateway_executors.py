"""Варианты исполнителя для селекта поля ввода (план 2026-07-28)."""

from svarog_harness.runtime.agents import (
    ADAPTER_BINARIES,
    EXTERNAL_ADAPTERS,
    adapter_available,
)


def test_registry_lists_every_adapter_with_its_binary() -> None:
    assert EXTERNAL_ADAPTERS == ("claude-code", "codex", "opencode")
    assert set(ADAPTER_BINARIES) == set(EXTERNAL_ADAPTERS)
    assert ADAPTER_BINARIES["claude-code"] == "claude"


def test_availability_is_a_path_lookup(monkeypatch) -> None:
    monkeypatch.setattr(
        "svarog_harness.runtime.agents.shutil.which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )
    assert adapter_available("claude-code") is True
    assert adapter_available("codex") is False
    assert adapter_available("нет-такого") is False
