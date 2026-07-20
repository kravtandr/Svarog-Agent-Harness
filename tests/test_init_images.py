"""Проверки сборки образов external executor при init."""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from svarog_harness.cli import init_images


def test_build_executor_image_uses_local_claude_dockerfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = tmp_path / "docker" / "agent-claude"
    context.mkdir(parents=True)
    (context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    commands: list[list[str]] = []
    monkeypatch.setattr(init_images, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(init_images, "find_docker", lambda: "/usr/bin/docker")

    def fake_run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        commands.append(command)
        return CompletedProcess(command, 0, "", "")

    class FakeBuild:
        def __init__(self) -> None:
            self.stdout = ["#1 building\n", "#1 DONE\n"]

        def wait(self, *, timeout: int) -> int:
            assert timeout == 900
            return 0

    monkeypatch.setattr(init_images.subprocess, "run", fake_run)
    monkeypatch.setattr(init_images.subprocess, "Popen", lambda *_args, **_kwargs: FakeBuild())

    progress: list[str] = []
    image = init_images.build_executor_image("claude-code", on_progress=progress.append)

    assert image == "svarog/agent-claude:latest"
    assert commands == [
        ["/usr/bin/docker", "info"],
    ]
    assert progress == ["#1 building", "#1 DONE"]


def test_build_executor_image_reports_docker_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(init_images, "find_docker", lambda: None)

    with pytest.raises(init_images.ExecutorImageBuildError, match="docker/podman"):
        init_images.build_executor_image("claude-code")


def test_build_executor_image_reports_unavailable_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = tmp_path / "docker" / "agent-claude"
    context.mkdir(parents=True)
    (context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setattr(init_images, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(init_images, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(
        init_images.subprocess,
        "run",
        lambda command, **_kwargs: CompletedProcess(command, 1, "", "daemon is not running"),
    )

    with pytest.raises(init_images.ExecutorImageBuildError, match="daemon не запущен"):
        init_images.build_executor_image("claude-code")
