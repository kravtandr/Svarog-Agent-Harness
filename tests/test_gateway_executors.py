"""Варианты исполнителя для селекта поля ввода (план 2026-07-28)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app
from svarog_harness.gateway.executors import executor_options
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


def test_unknown_adapter_is_rejected_even_with_custom_image(tmp_path: Path) -> None:
    """Регрессия: с кастомным образом ветка подмены образа не выполняется,
    и без отдельной проверки неизвестное имя адаптера уходило прямиком в
    model_copy — мимо любой валидации (pydantic не перепроверяет Literal
    при model_copy)."""
    cfg = _config(tmp_path, image="registry.example/custom:1")
    with pytest.raises(OverrideError, match="totally-bogus-adapter"):
        apply_override(cfg, RunOverride(executor="external", adapter="totally-bogus-adapter"))


def test_adapter_only_override_on_already_external_config(tmp_path: Path) -> None:
    """executor не задан в override — исполнитель берётся из cfg (уже
    external), но адаптер и образ всё равно должны подмениться."""
    cfg = _config(tmp_path)
    derived = apply_override(cfg, RunOverride(adapter="opencode"))
    assert derived.executor.external.adapter == "opencode"
    assert derived.executor.external.image == DEFAULT_OPENCODE_IMAGE


# --- список исполнителей для селекта (задача 3) ---------------------------


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayService:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _config(tmp_path)
    return GatewayService(cfg, tmp_path / "ws")


def test_native_always_present_and_active_matches_config(tmp_path: Path) -> None:
    cfg = _config(tmp_path)  # executor.type = external, adapter = claude-code
    options = executor_options(cfg)
    by_value = {o.value: o for o in options}
    assert by_value["native"].kind == "native"
    assert by_value["native"].available is True
    assert by_value["claude-code"].is_active is True
    assert by_value["native"].is_active is False


def test_configured_adapter_is_available_even_without_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("svarog_harness.gateway.executors.adapter_available", lambda _: False)
    options = {o.value: o for o in executor_options(_config(tmp_path))}
    assert options["claude-code"].available is True, "прописан в конфиге"
    assert options["opencode"].available is False
    assert "codex" in options, "недоступный адаптер показывается, а не прячется"


def test_executors_endpoint(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))
    body = client.get("/executors").json()
    assert body[0]["value"] == "native"
    assert all("available" in o and "is_active" in o for o in body)
