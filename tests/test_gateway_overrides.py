"""Override исполнителя/провайдера/модели в сообщении чата (план 2026-07-28)."""

from pathlib import Path

import pytest

from svarog_harness.config.loader import load_config
from svarog_harness.gateway.overrides import (
    OVERRIDE_META_KEY,
    OverrideError,
    RunOverride,
    apply_override,
)


def _config(tmp_path: Path, extra: str = "") -> object:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "    router:\n"
        "      base_url: https://openrouter.ai/api/v1\n"
        "      model: deepseek/deepseek-v4-flash\n"
        "      input_usd_per_mtok: 1.0\n"
        "      output_usd_per_mtok: 2.0\n"
        "sandbox:\n  type: local-trusted\n" + extra,
        encoding="utf-8",
    )
    # Исключить user-level конфиг, чтобы тесты не зависели от ~/.svarog/svarog.yaml.
    return load_config(project_dir=ws, user_config_path=tmp_path / "nonexistent")


def test_empty_override_returns_config_unchanged(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert RunOverride().is_empty()
    assert apply_override(cfg, RunOverride()) is cfg


def test_provider_switches_default(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    derived = apply_override(cfg, RunOverride(provider="router"))
    assert derived.models.default == "router"
    assert cfg.models.default == "local", "исходный конфиг не мутируется"


def test_model_applies_to_named_provider(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    derived = apply_override(cfg, RunOverride(provider="router", model="anthropic/claude"))
    assert derived.models.providers["router"].model == "anthropic/claude"
    assert derived.models.providers["local"].model == "fake-model"


def test_model_without_provider_applies_to_default(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    derived = apply_override(cfg, RunOverride(model="qwen3"))
    assert derived.models.providers["local"].model == "qwen3"


def test_prices_replace_provider_prices(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    derived = apply_override(
        cfg, RunOverride(provider="router", model="x/y"), prices=(0.5, 1.5)
    )
    assert derived.models.providers["router"].input_usd_per_mtok == 0.5
    assert derived.models.providers["router"].output_usd_per_mtok == 1.5


def test_unknown_provider_rejected_with_known_names(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(OverrideError) as exc:
        apply_override(cfg, RunOverride(provider="нет-такого"))
    assert "local" in str(exc.value) and "router" in str(exc.value)


def test_external_without_section_rejected(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(OverrideError, match="executor.external"):
        apply_override(cfg, RunOverride(executor="external"))


def test_external_requires_docker_sandbox(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        "executor:\n  type: native\n  external:\n    image: svarog/agent:1\n",
    )
    with pytest.raises(OverrideError, match="sandbox"):
        apply_override(cfg, RunOverride(executor="external"))


def test_external_allowed_with_docker(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: docker\n"
        "executor:\n  type: native\n  external:\n    image: svarog/agent:1\n",
        encoding="utf-8",
    )
    cfg = load_config(project_dir=ws, user_config_path=tmp_path / "nonexistent")
    derived = apply_override(cfg, RunOverride(executor="external"))
    assert derived.executor.type == "external"


def test_meta_round_trip_keeps_only_set_fields() -> None:
    ov = RunOverride(provider="router", model="x/y")
    meta = {OVERRIDE_META_KEY: ov.to_meta()}
    assert ov.to_meta() == {"provider": "router", "model": "x/y"}
    assert RunOverride.from_meta(meta) == ov
    assert RunOverride.from_meta(None).is_empty()
    assert RunOverride.from_meta({}).is_empty()
    assert RunOverride.from_meta({OVERRIDE_META_KEY: {"мусор": 1}}).is_empty()
