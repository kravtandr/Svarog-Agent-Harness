"""Применение /executor /mode /policies и запись в project svarog.yaml."""

from pathlib import Path

import pytest
import yaml

from svarog_harness.cli.chat_settings import (
    SettingsApplyError,
    apply_executor_label,
    apply_mode,
    apply_policies,
    patch_project_config,
)
from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import AutonomyMode, SvarogConfig
from svarog_harness.scaffold import DEFAULT_CLAUDE_IMAGE, DEFAULT_OPENCODE_IMAGE


def _cfg(tmp_path: Path, body: str) -> SvarogConfig:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(body, encoding="utf-8")
    return load_config(project_dir=ws)


def _base(tmp_path: Path, extra: str = "") -> str:
    return (
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n"
        f"{extra}"
    )


def test_apply_executor_native_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(tmp_path, _base(tmp_path, "sandbox:\n  type: docker\n"))
    updated = apply_executor_label(cfg, "native/local")
    assert updated.executor.type == "native"
    assert updated.sandbox.type == "local-trusted"


def test_apply_executor_external_requires_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(tmp_path, _base(tmp_path, "sandbox:\n  type: docker\n"))
    with pytest.raises(SettingsApplyError, match=r"executor\.external"):
        apply_executor_label(cfg, "external/claude-code")


def test_apply_executor_external_changes_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(
        tmp_path,
        _base(
            tmp_path,
            "sandbox:\n  type: docker\n"
            "executor:\n"
            "  type: external\n"
            "  external:\n"
            "    adapter: claude-code\n"
            "    image: svarog/claude:test\n"
            "    base_url: https://openrouter.ai/api\n",
        ),
    )
    updated = apply_executor_label(cfg, "external/codex")
    assert updated.executor.type == "external"
    assert updated.executor.external is not None
    assert updated.executor.external.adapter == "codex"
    assert updated.executor.external.image == "svarog/claude:test"


def test_apply_executor_switches_default_image_with_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Дефолтный образ должен переключаться вместе с adapter — иначе в sandbox
    остаётся образ прежнего агента и запуск падает `command not found`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(
        tmp_path,
        _base(
            tmp_path,
            "sandbox:\n  type: docker\n"
            "executor:\n"
            "  type: external\n"
            "  external:\n"
            "    adapter: opencode\n"
            f"    image: {DEFAULT_OPENCODE_IMAGE}\n"
            "    base_url: https://openrouter.ai/api\n"
            "    model: fake-model\n",
        ),
    )
    updated = apply_executor_label(cfg, "external/claude-code")
    assert updated.executor.external is not None
    assert updated.executor.external.adapter == "claude-code"
    assert updated.executor.external.image == DEFAULT_CLAUDE_IMAGE


def test_apply_mode_local_and_cloud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(
        tmp_path,
        _base(
            tmp_path,
            "sandbox:\n  type: docker\n"
            "executor:\n"
            "  type: external\n"
            "  external:\n"
            "    adapter: claude-code\n"
            "    image: svarog/claude:test\n",
        ),
    )
    local = apply_mode(cfg, "локальный loop")
    assert local.executor.type == "native"
    cloud = apply_mode(local, "cloud-агент")
    assert cloud.executor.type == "external"
    assert cloud.executor.external is not None
    assert cloud.executor.external.adapter == "claude-code"


def test_apply_policies_updates_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(tmp_path, _base(tmp_path))
    updated, autonomy = apply_policies(cfg, "auto")
    assert autonomy is AutonomyMode.AUTO
    assert updated.runtime.autonomy is AutonomyMode.AUTO


def test_patch_project_config_merges_and_preserves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        _base(tmp_path, "sandbox:\n  type: docker\nruntime:\n  autonomy: yolo\n"),
        encoding="utf-8",
    )
    path = patch_project_config(
        ws,
        {
            "runtime": {"autonomy": "supervised"},
            "executor": {"type": "native"},
        },
    )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["runtime"]["autonomy"] == "supervised"
    assert raw["executor"]["type"] == "native"
    assert raw["sandbox"]["type"] == "docker"
    assert raw["models"]["default"] == "local"


def test_apply_executor_invalid_swap_raises_settings_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Свап адаптера на wire=openai при anthropic base_url — SettingsApplyError,
    а не голый ValidationError: chat-сессия обязана пережить отказ (S15c)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(
        tmp_path,
        _base(
            tmp_path,
            "sandbox:\n  type: docker\n"
            "executor:\n"
            "  type: external\n"
            "  external:\n"
            "    adapter: claude-code\n"
            "    image: svarog/claude:test\n",
        ),
    )
    with pytest.raises(SettingsApplyError, match="openai"):
        apply_executor_label(cfg, "external/opencode")
