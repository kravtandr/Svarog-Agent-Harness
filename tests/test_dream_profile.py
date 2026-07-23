"""Тесты профиля Dream (блок C §6): реестр инструментов урезан структурно."""

from pathlib import Path

import pytest

from svarog_harness.config.schema import SvarogConfig
from svarog_harness.runtime.orchestrator import RunProfile, TaskRunner
from svarog_harness.sandbox.local import LocalEnvironment

# Инструменты, которых у Dream быть не должно. Проверяем поимённо, а не по
# количеству: иначе тест сломается от каждого нового инструмента в проекте.
FORBIDDEN = ("remember", "bash", "write_file", "edit_file", "spawn_child_run", "update_plan")


def _runner(tmp_path: Path) -> TaskRunner:
    cfg = SvarogConfig.model_validate(
        {
            "models": {
                "default": "main",
                "providers": {"main": {"base_url": "http://localhost", "model": "m"}},
            },
            "memory": {"path": str(tmp_path / "memory")},
            "storage": {"db_path": str(tmp_path / "svarog.sqlite3")},
        }
    )
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    return TaskRunner(cfg, tmp_path)


def _names(tmp_path: Path, profile: RunProfile) -> list[str]:
    runner = _runner(tmp_path)
    registry = runner._build_registry(
        LocalEnvironment(tmp_path),
        [],
        [],
        [],
        [],
        None,
        None,
        mem_dir=tmp_path / "memory",
        memory_proposal_sink=[],
        profile=profile,
    )
    return registry.names()


@pytest.fixture
def dream_registry_names(tmp_path: Path) -> list[str]:
    return _names(tmp_path, RunProfile.DREAM)


@pytest.fixture
def default_registry_names(tmp_path: Path) -> list[str]:
    return _names(tmp_path, RunProfile.DEFAULT)


def test_dream_registry_has_only_memory_tools(dream_registry_names: list[str]) -> None:
    assert "read_memory" in dream_registry_names
    assert "propose_memory_change" in dream_registry_names


def test_dream_registry_excludes_writing_tools(dream_registry_names: list[str]) -> None:
    """Dream читает содержимое из внешних источников — shell и запись ему не даём."""
    assert [name for name in FORBIDDEN if name in dream_registry_names] == []


def test_default_profile_keeps_remember(default_registry_names: list[str]) -> None:
    """Обычный run не теряет прямую запись: Flow A не меняется (ADR-0003)."""
    assert "remember" in default_registry_names
    assert "propose_memory_change" not in default_registry_names


def test_profiles_are_distinct() -> None:
    assert RunProfile.DEFAULT != RunProfile.DREAM
