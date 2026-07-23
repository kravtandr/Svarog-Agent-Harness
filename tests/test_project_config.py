"""Общий хелпер project-config ``svarog.yaml``.

Модуль нейтрален к вызывающему: бросает ProjectConfigError, а CLI и TUI
переводят его в своё исключение (typer.BadParameter / SettingsApplyError).
"""

from pathlib import Path

import pytest

from svarog_harness.common.project_config import (
    ProjectConfigError,
    read_project_config,
    write_yaml,
)


def test_missing_file_gives_empty_mapping(tmp_path: Path) -> None:
    assert read_project_config(tmp_path / "нет.yaml") == {}


def test_empty_file_gives_empty_mapping(tmp_path: Path) -> None:
    path = tmp_path / "svarog.yaml"
    path.write_text("", encoding="utf-8")
    assert read_project_config(path) == {}


def test_reads_mapping(tmp_path: Path) -> None:
    path = tmp_path / "svarog.yaml"
    path.write_text("runtime:\n  autonomy: yolo\n", encoding="utf-8")
    assert read_project_config(path) == {"runtime": {"autonomy": "yolo"}}


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "svarog.yaml"
    path.write_text("runtime: [незакрытый\n", encoding="utf-8")
    with pytest.raises(ProjectConfigError):
        read_project_config(path)


def test_non_mapping_top_level_raises(tmp_path: Path) -> None:
    path = tmp_path / "svarog.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ProjectConfigError):
        read_project_config(path)


def test_write_is_atomic_and_leaves_no_temp(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "svarog.yaml"
    write_yaml(path, {"memory": {"path": "./память"}})
    assert read_project_config(path) == {"memory": {"path": "./память"}}
    # Атомарность: временный файл не остаётся даже при вложенном каталоге.
    assert [p.name for p in path.parent.iterdir()] == ["svarog.yaml"]


def test_write_keeps_unicode_and_order(tmp_path: Path) -> None:
    path = tmp_path / "svarog.yaml"
    write_yaml(path, {"b": "тест", "a": 1})
    text = path.read_text(encoding="utf-8")
    assert "тест" in text
    assert text.index("b:") < text.index("a:")
