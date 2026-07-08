"""Тесты scaffolding agent-home (#19, §8) и команды svarog init."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from svarog_harness.cli import main as cli_main
from svarog_harness.config.loader import load_config
from svarog_harness.scaffold import scaffold_agent_home
from svarog_harness.secrets import is_secret_path
from svarog_harness.skills import scan_skills

runner = CliRunner()


def test_scaffold_creates_structure(tmp_path: Path) -> None:
    result = scaffold_agent_home(tmp_path)
    created = {p.relative_to(tmp_path).as_posix() for p in result.created}
    assert "AGENTS.md" in created
    assert "svarog.yaml" in created
    assert ".gitignore" in created
    assert "policies/security.yaml" in created
    assert "skills/example-note/SKILL.md" in created
    assert "memory/user/profile.md" in created
    assert (tmp_path / "workspaces/tasks/.gitkeep").exists()
    assert (tmp_path / "artifacts/.gitkeep").exists()


def test_scaffold_gitignore_covers_secrets(tmp_path: Path) -> None:
    scaffold_agent_home(tmp_path)
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore
    assert "*.pem" in gitignore
    # denylist и .gitignore согласованы.
    assert is_secret_path(".env")


def test_scaffold_config_is_loadable(tmp_path: Path) -> None:
    scaffold_agent_home(tmp_path)
    cfg = load_config(project_dir=tmp_path, user_config_path=tmp_path / "no-user.yaml")
    assert cfg.models.default == "local"
    assert cfg.memory.path is not None


def test_scaffold_example_skill_is_valid(tmp_path: Path) -> None:
    scaffold_agent_home(tmp_path)
    scan = scan_skills([tmp_path / "skills"])
    assert scan.errors == []
    assert [s.name for s in scan.skills] == ["example-note"]
    assert scan.skills[0].metadata.provenance == "official"


def test_scaffold_skips_existing_without_force(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("моё", encoding="utf-8")
    result = scaffold_agent_home(tmp_path)
    assert any(p.name == "AGENTS.md" for p in result.skipped)
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "моё"

    forced = scaffold_agent_home(tmp_path, force=True)
    assert any(p.name == "AGENTS.md" for p in forced.created)
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") != "моё"


def test_init_command_creates_home_and_memory_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    home = tmp_path / "home"
    home.mkdir()
    result = runner.invoke(cli_main.app, ["init", str(home)])
    assert result.exit_code == 0, result.output
    assert (home / "svarog.yaml").exists()
    # memory-репозиторий инициализирован (Flow A).
    assert (home / "memory" / ".git").is_dir()


def test_scaffold_writes_model_endpoint(tmp_path: Path) -> None:
    scaffold_agent_home(
        tmp_path, model="qwen2.5", base_url="https://openrouter.ai/api/v1", api_key_ref="MY_KEY"
    )
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "model: qwen2.5" in yaml
    assert "base_url: https://openrouter.ai/api/v1" in yaml
    # активный api_key_ref (не закомментирован).
    assert "\n      api_key_ref: MY_KEY" in yaml


def test_scaffold_config_omits_key_ref_by_default(tmp_path: Path) -> None:
    scaffold_agent_home(tmp_path)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    # без ключа строка остаётся закомментированной.
    assert "# api_key_ref:" in yaml


def test_init_stores_api_key_outside_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        ["init", str(home), "--no-input", "--api-key", "sk-secret-xyz", "--model", "m"],
    )
    assert result.exit_code == 0, result.output
    yaml = (home / "svarog.yaml").read_text(encoding="utf-8")
    # ключ не попал в конфиг, только ссылка.
    assert "sk-secret-xyz" not in yaml
    assert "api_key_ref: PROVIDER_API_KEY" in yaml
    # значение сохранено в secret store (~/.svarog/secrets.json).
    secrets = (tmp_path / "fakehome" / ".svarog" / "secrets.json").read_text(encoding="utf-8")
    assert "sk-secret-xyz" in secrets


def test_init_adds_home_to_project_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli_main.app, ["init", "agent-home", "--no-input"])
    assert result.exit_code == 0, result.output
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "agent-home/" in gitignore.splitlines()
