"""Тесты Flow C (§6.8, ADR-0003): task branch, commit по шагам, secret scan, push."""

from pathlib import Path

import pytest

from svarog_harness.config.schema import GitConfig
from svarog_harness.gitflow.commit_gate import SecretScanBlockedError
from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.gitflow.workspace import WorkspaceFlow, task_branch_name


async def _init_repo(path: Path) -> GitRepo:
    path.mkdir(parents=True, exist_ok=True)
    repo = GitRepo(path)
    await repo.init()
    await repo.ensure_identity()
    return repo


async def _seed_commit(repo: GitRepo) -> None:
    (repo.path / "README.md").write_text("# проект\n", encoding="utf-8")
    await repo.add_all()
    await repo.commit("initial")


def test_task_branch_name_slugifies() -> None:
    name = task_branch_name("Создай HELLO.py быстро!")
    assert name.startswith("svarog/")
    assert " " not in name
    assert name.split("-")[-1]  # суффикс-id


async def test_start_creates_task_branch(tmp_path: Path) -> None:
    repo = await _init_repo(tmp_path / "ws")
    await _seed_commit(repo)
    flow = WorkspaceFlow(repo, GitConfig())

    prep = await flow.start("почини баг")
    assert prep.is_git
    assert prep.branch is not None
    assert prep.branch.startswith("svarog/")
    assert await repo.current_branch() == prep.branch


async def test_start_non_git(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    prep = await WorkspaceFlow(GitRepo(plain), GitConfig()).start("задача")
    assert not prep.is_git


async def test_commit_step_commits_changes(tmp_path: Path) -> None:
    repo = await _init_repo(tmp_path / "ws")
    await _seed_commit(repo)
    flow = WorkspaceFlow(repo, GitConfig())
    await flow.start("задача")

    (repo.path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    sha = await flow.commit_step("svarog: добавил app.py", run_id="run-1")
    assert sha is not None
    _, log, _ = await repo._git("log", "--format=%B", "-n", "1")
    assert "Run-Id: run-1" in log


async def test_planted_git_hook_not_executed_on_host(tmp_path: Path) -> None:
    """Reproducer 0.2 (ADR-0015): агент сажает .git/hooks/pre-commit, host-side
    commit его НЕ исполняет (core.hooksPath=/dev/null)."""
    repo = await _init_repo(tmp_path / "ws")
    await _seed_commit(repo)
    flow = WorkspaceFlow(repo, GitConfig())
    await flow.start("задача")

    sentinel = tmp_path / "hook-ran.txt"
    hook = repo.path / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
    hook.chmod(0o755)

    (repo.path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    sha = await flow.commit_step("svarog: с подсаженным hook", run_id="run-1")
    assert sha is not None
    # Коммит прошёл, но hook на хосте не выполнился.
    assert not sentinel.exists()


async def test_hardened_git_ignores_global_config(tmp_path: Path, monkeypatch) -> None:
    """Global git-config (алиасы/фильтры из ~/.gitconfig) не подхватывается."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".gitconfig").write_text(
        "[core]\n\thooksPath = /tmp/should-not-be-used\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    repo = await _init_repo(tmp_path / "ws")
    # hardened env выставляет GIT_CONFIG_GLOBAL=/dev/null — hooksPath из
    # ~/.gitconfig не подхватывается host-side git.
    _, out, _ = await repo._git("config", "--get", "core.hooksPath", check=False)
    assert "/tmp/should-not-be-used" not in out


async def test_commit_step_noop_when_clean(tmp_path: Path) -> None:
    repo = await _init_repo(tmp_path / "ws")
    await _seed_commit(repo)
    flow = WorkspaceFlow(repo, GitConfig())
    await flow.start("задача")
    assert await flow.commit_step("нечего коммитить") is None


async def test_commit_step_blocks_on_secret(tmp_path: Path) -> None:
    repo = await _init_repo(tmp_path / "ws")
    await _seed_commit(repo)
    flow = WorkspaceFlow(repo, GitConfig())
    await flow.start("задача")

    # Публичный AWS example — заведомо ненастоящий ключ.
    (repo.path / "deploy.sh").write_text("KEY=AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")
    with pytest.raises(SecretScanBlockedError):
        await flow.commit_step("svarog: деплой")


async def test_push_precheck_scans_committed_branch_when_staged_is_clean(tmp_path: Path) -> None:
    repo = await _init_repo(tmp_path / "ws")
    await _seed_commit(repo)
    flow = WorkspaceFlow(repo, GitConfig())
    prep = await flow.start("секрет уже в коммите")
    assert prep.branch is not None

    # Обходим commit_guarded намеренно: pre-push scan должен быть второй линией
    # защиты и ловить секреты в уже созданном коммите, когда staged area пустой.
    (repo.path / "leaked.txt").write_text("KEY=AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")
    await repo.add_all()
    await repo.commit("unsafe direct commit")
    assert not await repo.has_staged_changes()

    findings = await flow.push_precheck(prep.branch)
    assert [f.rule for f in findings] == ["aws-access-key-id"]


async def test_push_to_local_bare_remote(tmp_path: Path) -> None:
    """Push против локального bare-репозитория — без сети."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    await GitRepo(bare)._git("init", "--bare", "-b", "main")

    repo = await _init_repo(tmp_path / "ws")
    await _seed_commit(repo)
    await repo._git("remote", "add", "origin", str(bare))

    flow = WorkspaceFlow(repo, GitConfig())
    prep = await flow.start("новая фича")
    assert prep.branch is not None
    (repo.path / "feature.py").write_text("x = 1\n", encoding="utf-8")
    await flow.commit_step("svarog: фича")

    await flow.push(prep.branch)
    # Ветка появилась в bare-remote.
    _, branches, _ = await GitRepo(bare)._git("branch", "--list", prep.branch)
    assert prep.branch in branches


async def test_start_pulls_from_remote(tmp_path: Path) -> None:
    bare = tmp_path / "remote.git"
    bare.mkdir()
    await GitRepo(bare)._git("init", "--bare", "-b", "main")

    origin = await _init_repo(tmp_path / "origin")
    await _seed_commit(origin)
    await origin._git("remote", "add", "origin", str(bare))
    await origin._git("push", "-u", "origin", "main")

    # Клон, в который прилетит новый коммит из bare.
    clone_dir = tmp_path / "clone"
    await GitRepo(tmp_path)._git("clone", str(bare), str(clone_dir))
    clone = GitRepo(clone_dir)
    await clone.ensure_identity()

    # Второй писатель добавляет коммит в remote.
    (origin.path / "new.txt").write_text("свежее\n", encoding="utf-8")
    await origin.add_all()
    await origin.commit("второй коммит")
    await origin._git("push", "origin", "main")

    prep = await WorkspaceFlow(clone, GitConfig(auto_pull=True)).start("подтяни и работай")
    assert prep.pulled
    assert (clone_dir / "new.txt").exists()


async def test_commit_step_skips_svarog_tree(tmp_path: Path) -> None:
    """ADR-0015 §1.2: spill-каталог .svarog не попадает в коммиты Flow C."""
    repo = GitRepo(tmp_path)
    await repo.init()
    await repo.ensure_identity()
    flow = WorkspaceFlow(repo, GitConfig())

    (tmp_path / "code.py").write_text("print('ok')\n", encoding="utf-8")
    spill = tmp_path / ".svarog" / "tool-results" / "run" / "call.txt"
    spill.parent.mkdir(parents=True, exist_ok=True)
    spill.write_text("огромный вывод", encoding="utf-8")

    sha = await flow.commit_step("шаг")
    assert sha is not None
    _, out, _ = await repo._git("ls-tree", "-r", "--name-only", "HEAD")
    files = out.split()
    assert "code.py" in files
    assert not any(name.startswith(".svarog") for name in files)


async def test_commit_step_excludes_project_config(tmp_path: Path) -> None:
    """Project-конфиг (имена секретов) не принадлежит диффу run'а: попав в
    task-ветку, он к тому же исчезает из рабочего дерева при checkout master
    (кампания 21.07.2026, S11 Watch(6))."""
    repo = await _init_repo(tmp_path / "ws")
    await _seed_commit(repo)
    flow = WorkspaceFlow(repo, GitConfig())
    await flow.start("задача")

    (repo.path / "svarog.yaml").write_text("models: {}\n", encoding="utf-8")
    (repo.path / "result.md").write_text("работа\n", encoding="utf-8")
    sha = await flow.commit_step("svarog: задача")
    assert sha is not None
    _, out, _ = await repo._git("show", "--name-only", "--format=", sha)
    files = out.split()
    assert "result.md" in files
    assert "svarog.yaml" not in files
    # рабочее дерево конфиг не потеряло
    assert (repo.path / "svarog.yaml").exists()


async def test_commit_step_keeps_tracked_project_config(tmp_path: Path) -> None:
    """Если пользователь сам закоммитил свой svarog.yaml — поведение прежнее:
    info/exclude не действует на уже отслеживаемые файлы."""
    repo = await _init_repo(tmp_path / "ws")
    (repo.path / "svarog.yaml").write_text("models: {}\n", encoding="utf-8")
    (repo.path / "README.md").write_text("# проект\n", encoding="utf-8")
    await repo.add_all()
    await repo.commit("initial с конфигом")
    flow = WorkspaceFlow(repo, GitConfig())
    await flow.start("задача")

    (repo.path / "svarog.yaml").write_text("models: {a: 1}\n", encoding="utf-8")
    sha = await flow.commit_step("svarog: правка конфига")
    assert sha is not None
    _, out, _ = await repo._git("show", "--name-only", "--format=", sha)
    assert "svarog.yaml" in out.split()
