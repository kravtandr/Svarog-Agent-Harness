"""Варианты исполнителя для селекта поля ввода (план 2026-07-28)."""

from pathlib import Path

import pytest

from svarog_harness.config.loader import load_config
from svarog_harness.gateway.overrides import OverrideError, RunOverride, apply_override
from svarog_harness.runtime.agents import (
    ADAPTER_BINARIES,
    EXTERNAL_ADAPTERS,
    adapter_available,
)
from svarog_harness.scaffold import DEFAULT_CLAUDE_IMAGE, DEFAULT_OPENCODE_IMAGE


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


def _config(tmp_path: Path, image: str = DEFAULT_CLAUDE_IMAGE) -> object:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: docker\n"
        "executor:\n"
        "  type: external\n"
        "  external:\n"
        "    adapter: claude-code\n"
        f"    image: {image}\n",
        encoding="utf-8",
    )
    return load_config(project_dir=ws, user_config_path=tmp_path / "нет")


def test_adapter_switches_adapter_and_default_image(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    derived = apply_override(cfg, RunOverride(executor="external", adapter="opencode"))
    assert derived.executor.external.adapter == "opencode"
    assert derived.executor.external.image == DEFAULT_OPENCODE_IMAGE


def test_custom_image_is_left_alone(tmp_path: Path) -> None:
    cfg = _config(tmp_path, image="registry.example/мой-агент:7")
    derived = apply_override(cfg, RunOverride(executor="external", adapter="opencode"))
    assert derived.executor.external.adapter == "opencode"
    assert derived.executor.external.image == "registry.example/мой-агент:7"


def test_codex_without_own_image_is_rejected(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(OverrideError, match="codex"):
        apply_override(cfg, RunOverride(executor="external", adapter="codex"))


def test_codex_allowed_when_image_is_custom(tmp_path: Path) -> None:
    cfg = _config(tmp_path, image="registry.example/codex:1")
    derived = apply_override(cfg, RunOverride(executor="external", adapter="codex"))
    assert derived.executor.external.adapter == "codex"


def test_adapter_without_external_executor_is_rejected(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(OverrideError, match="native"):
        apply_override(cfg, RunOverride(executor="native", adapter="opencode"))


def test_adapter_round_trips_through_meta() -> None:
    ov = RunOverride(executor="external", adapter="opencode")
    assert ov.to_meta() == {"executor": "external", "adapter": "opencode"}
    assert RunOverride.from_meta({"override": ov.to_meta()}) == ov
    assert RunOverride.from_meta({"override": {"adapter": 42}}).adapter is None
