"""Тесты scaffolding agent-home (#19, §8) и команды svarog init."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from svarog_harness.cli import main as cli_main
from svarog_harness.config.loader import load_config
from svarog_harness.scaffold import (
    ClaudeExecutorSetup,
    ExecutorSetup,
    OpencodeExecutorSetup,
    scaffold_agent_home,
)
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
    assert ".svarog/" in gitignore
    assert "*.pem" in gitignore
    # denylist и .gitignore согласованы.
    assert is_secret_path(".env")
    assert is_secret_path(".svarog/svarog.db")


def test_scaffold_config_is_loadable(tmp_path: Path) -> None:
    scaffold_agent_home(tmp_path)
    cfg = load_config(project_dir=tmp_path, user_config_path=tmp_path / "no-user.yaml")
    assert cfg.models.default == "local"
    assert cfg.memory.path is not None
    assert cfg.storage.db_path == Path(".svarog/svarog.db")


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
    # memory-репозиторий инициализирован (Flow A); объекты git вне дерева
    # (separate-git-dir, ADR-0015 §0.2) — в дереве только файл-указатель `.git`.
    assert (home / "memory" / ".git").is_file()
    assert (home / ".gitdirs" / "memory").is_dir()


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


def test_scaffold_no_executor_omits_section(tmp_path: Path) -> None:
    scaffold_agent_home(tmp_path)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "executor:" not in yaml


def test_scaffold_claude_api_key_executor_block(tmp_path: Path) -> None:
    executor = ExecutorSetup(
        active="claude-code",
        claude=ClaudeExecutorSetup(
            auth="api-key", api_key_ref="CLAUDE_CODE_KEY", oauth_token_ref=None
        ),
    )
    scaffold_agent_home(tmp_path, executor=executor)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "executor:\n  type: external\n  external:\n    adapter: claude-code" in yaml
    assert "    image: svarog/agent-claude:latest" in yaml
    assert "    auth: api-key" in yaml
    assert "    api_key_ref: CLAUDE_CODE_KEY" in yaml
    assert "тоже настроен" not in yaml


def test_scaffold_claude_api_key_without_value_comments_ref(tmp_path: Path) -> None:
    executor = ExecutorSetup(
        active="claude-code",
        claude=ClaudeExecutorSetup(auth="api-key", api_key_ref=None, oauth_token_ref=None),
    )
    scaffold_agent_home(tmp_path, executor=executor)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "    # api_key_ref: CLAUDE_CODE_KEY" in yaml
    assert "\n    api_key_ref:" not in yaml


def test_scaffold_claude_subscription_ref_always_active(tmp_path: Path) -> None:
    executor = ExecutorSetup(
        active="claude-code",
        claude=ClaudeExecutorSetup(
            auth="subscription", api_key_ref=None, oauth_token_ref="CLAUDE_CODE_OAUTH_TOKEN"
        ),
    )
    scaffold_agent_home(tmp_path, executor=executor)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "    auth: subscription" in yaml
    assert "\n    oauth_token_ref: CLAUDE_CODE_OAUTH_TOKEN" in yaml


def test_scaffold_claude_subscription_without_ref_falls_back_to_default(tmp_path: Path) -> None:
    executor = ExecutorSetup(
        active="claude-code",
        claude=ClaudeExecutorSetup(
            auth="subscription", api_key_ref=None, oauth_token_ref=None
        ),
    )
    scaffold_agent_home(tmp_path, executor=executor)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "    auth: subscription" in yaml
    # Должна быть активная строка с дефолтным значением, не закомментирована и не "None"
    assert "\n    oauth_token_ref: CLAUDE_CODE_OAUTH_TOKEN" in yaml


def test_scaffold_opencode_own_creds(tmp_path: Path) -> None:
    executor = ExecutorSetup(
        active="opencode",
        opencode=OpencodeExecutorSetup(
            model="qwen3-coder",
            base_url="https://openrouter.ai/api/v1",
            api_key_ref="OPENCODE_API_KEY",
        ),
    )
    scaffold_agent_home(tmp_path, executor=executor)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "    adapter: opencode" in yaml
    assert "    image: svarog/agent-opencode:latest" in yaml
    assert "    model: qwen3-coder" in yaml
    assert "    base_url: https://openrouter.ai/api/v1" in yaml
    assert "    api_key_ref: OPENCODE_API_KEY" in yaml


def test_scaffold_opencode_without_ref_comments_line(tmp_path: Path) -> None:
    executor = ExecutorSetup(
        active="opencode",
        opencode=OpencodeExecutorSetup(
            model="qwen3-coder", base_url="http://localhost:8000/v1", api_key_ref=None
        ),
    )
    scaffold_agent_home(tmp_path, executor=executor)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "    # api_key_ref: OPENCODE_API_KEY" in yaml


def test_scaffold_standby_adapter_rendered_as_comment(tmp_path: Path) -> None:
    executor = ExecutorSetup(
        active="claude-code",
        claude=ClaudeExecutorSetup(
            auth="api-key", api_key_ref="CLAUDE_CODE_KEY", oauth_token_ref=None
        ),
        opencode=OpencodeExecutorSetup(
            model="qwen3-coder", base_url="http://localhost:8000/v1", api_key_ref="PROVIDER_API_KEY"
        ),
    )
    scaffold_agent_home(tmp_path, executor=executor)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "OpenCode тоже настроен и готов" in yaml
    assert "# executor:" in yaml
    assert "#   external:" in yaml
    assert "#     adapter: opencode" in yaml
    assert "#     api_key_ref: PROVIDER_API_KEY" in yaml
    # активный блок остаётся некомментированным
    assert "\n    adapter: claude-code" in yaml


def test_init_no_executor_flags_omits_executor_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    home = tmp_path / "home"
    result = runner.invoke(cli_main.app, ["init", str(home), "--no-input"])
    assert result.exit_code == 0, result.output
    yaml = (home / "svarog.yaml").read_text(encoding="utf-8")
    assert "executor:" not in yaml


def test_init_claude_api_key_writes_executor_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        [
            "init",
            str(home),
            "--no-input",
            "--executor",
            "claude-code",
            "--claude-auth",
            "api-key",
            "--claude-api-key",
            "sk-claude-x",
        ],
    )
    assert result.exit_code == 0, result.output
    yaml = (home / "svarog.yaml").read_text(encoding="utf-8")
    assert "adapter: claude-code" in yaml
    assert "auth: api-key" in yaml
    assert "api_key_ref: CLAUDE_CODE_KEY" in yaml
    assert "sk-claude-x" not in yaml
    secrets = (tmp_path / "fakehome" / ".svarog" / "secrets.json").read_text(encoding="utf-8")
    assert "sk-claude-x" in secrets


def test_init_claude_subscription_without_token_reminds_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        [
            "init",
            str(home),
            "--no-input",
            "--executor",
            "claude-code",
            "--claude-auth",
            "subscription",
        ],
    )
    assert result.exit_code == 0, result.output
    yaml = (home / "svarog.yaml").read_text(encoding="utf-8")
    assert "auth: subscription" in yaml
    assert "oauth_token_ref: CLAUDE_CODE_OAUTH_TOKEN" in yaml
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.output  # напоминание сохранить токен
    secrets_path = tmp_path / "fakehome" / ".svarog" / "secrets.json"
    assert not secrets_path.exists() or "CLAUDE_CODE_OAUTH_TOKEN" not in secrets_path.read_text(
        encoding="utf-8"
    )


def test_init_opencode_same_as_native_reuses_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        [
            "init",
            str(home),
            "--no-input",
            "--model",
            "m",
            "--base-url",
            "http://localhost:9000/v1",
            "--api-key",
            "sk-native-x",
            "--executor",
            "opencode",
            "--opencode-same-as-native",
        ],
    )
    assert result.exit_code == 0, result.output
    yaml = (home / "svarog.yaml").read_text(encoding="utf-8")
    assert "adapter: opencode" in yaml
    assert "model: m" in yaml
    assert "base_url: http://localhost:9000/v1" in yaml
    # OpenCode ссылается на тот же ref, что и нативная модель — не создаёт новый
    assert "OPENCODE_API_KEY" not in yaml
    assert yaml.count("api_key_ref: PROVIDER_API_KEY") == 2  # models + executor


def test_init_opencode_own_creds_writes_separate_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        [
            "init",
            str(home),
            "--no-input",
            "--executor",
            "opencode",
            "--opencode-own-creds",
            "--opencode-model",
            "m2",
            "--opencode-base-url",
            "http://localhost:9100/v1",
            "--opencode-api-key",
            "sk-opencode-y",
        ],
    )
    assert result.exit_code == 0, result.output
    yaml = (home / "svarog.yaml").read_text(encoding="utf-8")
    assert "model: m2" in yaml
    assert "api_key_ref: OPENCODE_API_KEY" in yaml
    secrets = (tmp_path / "fakehome" / ".svarog" / "secrets.json").read_text(encoding="utf-8")
    assert "sk-opencode-y" in secrets


def test_init_both_adapters_writes_standby_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        [
            "init",
            str(home),
            "--no-input",
            "--executor",
            "claude-code",
            "--claude-auth",
            "api-key",
            "--claude-api-key",
            "sk-claude-x",
            "--opencode-own-creds",
            "--opencode-model",
            "m2",
        ],
    )
    assert result.exit_code == 0, result.output
    yaml = (home / "svarog.yaml").read_text(encoding="utf-8")
    assert "\n    adapter: claude-code" in yaml
    assert "тоже настроен и готов" in yaml
    assert "#     adapter: opencode" in yaml


def test_init_both_adapters_without_executor_flag_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        [
            "init",
            str(home),
            "--no-input",
            "--claude-api-key",
            "sk-claude-x",
            "--opencode-model",
            "m2",
        ],
    )
    assert result.exit_code != 0


def test_init_conflicting_opencode_creds_flags_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        [
            "init",
            str(home),
            "--no-input",
            "--opencode-same-as-native",
            "--opencode-own-creds",
        ],
    )
    assert result.exit_code != 0


def test_init_executor_native_with_claude_flags_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        ["init", str(home), "--no-input", "--executor", "native", "--claude-api-key", "sk-x"],
    )
    assert result.exit_code != 0
