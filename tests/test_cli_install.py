"""Тесты `svarog install`: env-блок в rc + symlink на user-конфиг."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from svarog_harness.cli import install as install_module
from svarog_harness.cli.main import app

runner = CliRunner()


# --- render_rc_block --------------------------------------------------------


def test_render_rc_block_contains_all_exports_and_alias(tmp_path: Path) -> None:
    repo = tmp_path / "Svarog-Agent-Harness"
    home = tmp_path / "agent-home"
    block = install_module.render_rc_block(repo, home)

    assert install_module._BLOCK_BEGIN in block
    assert install_module._BLOCK_END in block
    assert f'export SVAROG_REPO="{repo.as_posix()}"' in block
    assert f'export SVAROG_AGENT_HOME="{home.as_posix()}"' in block
    assert f'export SVAROG_MEMORY__PATH="{home.as_posix()}/memory"' in block
    assert f'export SVAROG_STORAGE__DB_PATH="{home.as_posix()}/.svarog/svarog.db"' in block
    assert f'export SVAROG_SKILLS__PATHS="["{home.as_posix()}/skills"]"' in block
    # alias ссылается на $SVAROG_REPO (раскрывается shell'ом, не на момент install).
    assert "alias svarog='uv --project \"$SVAROG_REPO\" run svarog'" in block


# --- install_to_rc ----------------------------------------------------------


def test_install_to_rc_creates_file_when_missing(tmp_path: Path) -> None:
    rc = tmp_path / ".bashrc"
    block = install_module.render_rc_block(tmp_path / "repo", tmp_path / "home")

    assert install_module.install_to_rc(rc, block) is True
    assert block in rc.read_text(encoding="utf-8")


def test_install_to_rc_is_idempotent(tmp_path: Path) -> None:
    rc = tmp_path / ".bashrc"
    block = install_module.render_rc_block(tmp_path / "repo", tmp_path / "home")

    install_module.install_to_rc(rc, block)
    again = install_module.install_to_rc(rc, block)

    assert again is False
    text = rc.read_text(encoding="utf-8")
    # ровно одно вхождение блока.
    assert text.count(install_module._BLOCK_BEGIN) == 1
    assert text.count(install_module._BLOCK_END) == 1


def test_install_to_rc_replaces_old_block_on_change(tmp_path: Path) -> None:
    rc = tmp_path / ".bashrc"
    rc.write_text("alias ll='ls -la'\n", encoding="utf-8")
    old_block = install_module.render_rc_block(tmp_path / "old-repo", tmp_path / "old-home")
    install_module.install_to_rc(rc, old_block)

    new_block = install_module.render_rc_block(tmp_path / "new-repo", tmp_path / "new-home")
    changed = install_module.install_to_rc(rc, new_block)

    assert changed is True
    text = rc.read_text(encoding="utf-8")
    assert "old-repo" not in text
    assert "new-repo" in text
    assert text.count(install_module._BLOCK_BEGIN) == 1
    # чужой alias сохранён.
    assert "alias ll='ls -la'" in text


# --- link_user_config -------------------------------------------------------


def _setup_home(tmp_path: Path) -> tuple[Path, Path]:
    """Изолированный HOME с user-конфигом, указующим на agent-home."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    agent_home = tmp_path / "agent-home"
    (agent_home / ".svarog").mkdir(parents=True)
    (agent_home / "svarog.yaml").write_text("models: {}\n", encoding="utf-8")
    return fake_home, agent_home


def test_link_user_config_creates_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home, agent_home = _setup_home(tmp_path)
    target = fake_home / ".svarog" / "svarog.yaml"
    monkeypatch.setattr(install_module, "USER_CONFIG_PATH", target)

    changed, reason = install_module.link_user_config(agent_home)
    assert (changed, reason) == (True, "linked")
    assert target.is_symlink()
    assert Path(target.readlink()) == agent_home / "svarog.yaml"


def test_link_user_config_refuses_to_clobber_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home, agent_home = _setup_home(tmp_path)
    svarog_dir = fake_home / ".svarog"
    svarog_dir.mkdir()
    # Имитируем, что `svarog login` уже создал regular file.
    target = svarog_dir / "svarog.yaml"
    target.write_text("remote:\n  url: https://example\n", encoding="utf-8")
    monkeypatch.setattr(install_module, "USER_CONFIG_PATH", target)

    changed, reason = install_module.link_user_config(agent_home)

    assert (changed, reason) == (False, "exists-regular")
    # regular file не затёрт.
    assert not target.is_symlink()
    assert "remote" in target.read_text(encoding="utf-8")


def test_link_user_config_already_linked_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home, agent_home = _setup_home(tmp_path)
    svarog_dir = fake_home / ".svarog"
    svarog_dir.mkdir()
    target = svarog_dir / "svarog.yaml"
    target.symlink_to(agent_home / "svarog.yaml")
    monkeypatch.setattr(install_module, "USER_CONFIG_PATH", target)

    changed, reason = install_module.link_user_config(agent_home)

    assert (changed, reason) == (False, "already-linked")


# --- CLI end-to-end ---------------------------------------------------------


def _bootstrap_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Минимальный checkout Svarog: repo/pyproject.toml + repo/agent-home/svarog.yaml."""
    repo = tmp_path / "Svarog-Agent-Harness"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'svarog-harness'\n", encoding="utf-8")
    home = repo / "agent-home"
    home.mkdir()
    (home / "svarog.yaml").write_text("models: {}\n", encoding="utf-8")
    return repo, home


def test_cli_install_writes_rc_and_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, home = _bootstrap_repo(tmp_path)
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    svarog_dir = fake_home / ".svarog"
    rc = fake_home / ".bashrc"

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(install_module, "USER_CONFIG_PATH", svarog_dir / "svarog.yaml")
    # `_rc_path` использует expanduser() → после monkeypatch HOME это fake_home/.bashrc.
    monkeypatch.chdir(repo)

    result = runner.invoke(
        app,
        ["install", "--agent-home", str(home), "--shell", "bash"],
        # CliRunner изолирует stdio, но не HOME — monkeypatch выше covers it.
    )

    assert result.exit_code == 0, result.output
    text = rc.read_text(encoding="utf-8")
    assert 'export SVAROG_REPO="' in text
    assert "alias svarog=" in text
    assert (svarog_dir / "svarog.yaml").is_symlink()


def test_cli_install_idempotent_second_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, home = _bootstrap_repo(tmp_path)
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    svarog_dir = fake_home / ".svarog"
    rc = fake_home / ".bashrc"

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(install_module, "USER_CONFIG_PATH", svarog_dir / "svarog.yaml")
    monkeypatch.chdir(repo)

    runner.invoke(app, ["install", "--agent-home", str(home), "--shell", "bash"])
    first = rc.read_text(encoding="utf-8")

    result = runner.invoke(app, ["install", "--agent-home", str(home), "--shell", "bash"])
    second = rc.read_text(encoding="utf-8")

    assert result.exit_code == 0, result.output
    assert first == second  # блок не дублировался
    assert second.count(install_module._BLOCK_BEGIN) == 1


def test_cli_install_fails_when_agent_home_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = _bootstrap_repo(tmp_path)
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.chdir(repo)

    result = runner.invoke(
        app, ["install", "--agent-home", str(tmp_path / "nope"), "--shell", "bash"]
    )

    assert result.exit_code == 1
    assert "не найден" in result.output
    assert "svarog init" in result.output


def test_cli_install_skips_symlink_on_existing_regular(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, home = _bootstrap_repo(tmp_path)
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    svarog_dir = fake_home / ".svarog"
    svarog_dir.mkdir()
    target = svarog_dir / "svarog.yaml"
    target.write_text("remote:\n  url: https://x\n", encoding="utf-8")  # как после svarog login

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(install_module, "USER_CONFIG_PATH", target)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["install", "--agent-home", str(home), "--shell", "bash"])

    assert result.exit_code == 0, result.output
    # rc всё равно записан.
    assert (fake_home / ".bashrc").read_text(encoding="utf-8").count(
        install_module._BLOCK_BEGIN
    ) == 1
    # regular file не тронут.
    assert not target.is_symlink()
    assert "remote" in target.read_text(encoding="utf-8")
    assert "уже существует" in result.output
