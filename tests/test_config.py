from pathlib import Path

import pytest

from svarog_harness.config.loader import ConfigError, deep_merge, load_config
from svarog_harness.config.schema import AutonomyMode

MINIMAL_MODELS = """\
models:
  default: local-qwen
  providers:
    local-qwen:
      type: openai-compatible
      base_url: http://localhost:8000/v1
      model: qwen3-coder
"""


def write_project_config(tmp_path: Path, content: str) -> Path:
    (tmp_path / "svarog.yaml").write_text(content, encoding="utf-8")
    return tmp_path


def load(tmp_path: Path, user_content: str | None = None):
    user_path = tmp_path / "user-svarog.yaml"
    if user_content is not None:
        user_path.write_text(user_content, encoding="utf-8")
    return load_config(project_dir=tmp_path, user_config_path=user_path)


def test_minimal_config_gets_defaults(tmp_path: Path) -> None:
    write_project_config(tmp_path, MINIMAL_MODELS)
    config = load(tmp_path)
    assert config.runtime.autonomy is AutonomyMode.YOLO
    assert config.runtime.max_iterations == 50
    assert config.sandbox.network == "disabled"
    assert config.git.secret_scan_before_commit is True
    assert config.policies.protected_branches == ["main", "production"]
    assert config.models.auxiliary_or_default == "local-qwen"


def test_project_deep_merges_over_user(tmp_path: Path) -> None:
    user = MINIMAL_MODELS + "runtime:\n  max_iterations: 80\n  refuel_after_iterations: 60\n"
    project = "runtime:\n  autonomy: supervised\n"
    write_project_config(tmp_path, project)
    config = load(tmp_path, user_content=user)
    # project задал только autonomy — остальная секция runtime из user-файла сохранилась
    assert config.runtime.autonomy is AutonomyMode.SUPERVISED
    assert config.runtime.max_iterations == 80
    assert config.runtime.refuel_after_iterations == 60


def test_env_overrides_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_project_config(tmp_path, MINIMAL_MODELS + "runtime:\n  autonomy: yolo\n")
    monkeypatch.setenv("SVAROG_RUNTIME__AUTONOMY", "supervised")
    config = load(tmp_path)
    assert config.runtime.autonomy is AutonomyMode.SUPERVISED


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    write_project_config(tmp_path, MINIMAL_MODELS + "runtme:\n  autonomy: yolo\n")
    with pytest.raises(ConfigError, match="runtme"):
        load(tmp_path)


def test_default_model_must_be_defined_provider(tmp_path: Path) -> None:
    config = MINIMAL_MODELS.replace("default: local-qwen", "default: missing-model")
    write_project_config(tmp_path, config)
    with pytest.raises(ConfigError, match="missing-model"):
        load(tmp_path)


def test_refuel_threshold_above_max_disables_refuel(tmp_path: Path) -> None:
    # refuel_after_iterations >= max_iterations допустимо: порог недостижим,
    # refuel просто отключён (§6.10) — не ошибка конфигурации.
    write_project_config(
        tmp_path,
        MINIMAL_MODELS + "runtime:\n  max_iterations: 10\n  refuel_after_iterations: 10\n",
    )
    config = load(tmp_path)
    assert config.runtime.refuel_after_iterations == 10
    assert config.runtime.max_iterations == 10


def test_missing_config_reports_searched_paths(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="models"):
        load(tmp_path)


def test_invalid_yaml_reports_file(tmp_path: Path) -> None:
    write_project_config(tmp_path, "models: [unclosed\n")
    with pytest.raises(ConfigError, match="YAML"):
        load(tmp_path)


def test_deep_merge_replaces_lists_and_scalars() -> None:
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1}
    override = {"a": {"y": [3]}, "c": 2}
    assert deep_merge(base, override) == {"a": {"x": 1, "y": [3]}, "b": 1, "c": 2}


def test_developer_svarog_env_does_not_leak_into_tests() -> None:
    """Окружение разработчика не должно подменять tmp-пути теста.

    Autouse-фикстура в conftest снимает SVAROG_*; без неё `load_config` в
    тесте возвращал реальные agent-home, БД и каталог скиллов.
    """
    import os

    assert [name for name in os.environ if name.startswith("SVAROG_")] == []
