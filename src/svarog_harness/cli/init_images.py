"""Сборка образов external executor во время ``svarog init``."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from svarog_harness.sandbox.docker import find_docker
from svarog_harness.scaffold import DEFAULT_CLAUDE_IMAGE, DEFAULT_OPENCODE_IMAGE

ExecutorAdapter = Literal["claude-code", "opencode"]


class ExecutorImageBuildError(RuntimeError):
    """Не удалось собрать локальный образ выбранного external executor."""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def build_executor_image(
    adapter: ExecutorAdapter, *, on_progress: Callable[[str], None] | None = None
) -> str:
    """Собрать образ executor и передавать строки Docker BuildKit в callback."""
    docker = find_docker()
    if docker is None:
        raise ExecutorImageBuildError("docker/podman не найден")

    try:
        status = subprocess.run(
            [docker, "info"], check=False, capture_output=True, text=True, timeout=15
        )
    except subprocess.TimeoutExpired as exc:
        raise ExecutorImageBuildError(
            "Docker найден, но daemon не отвечает; запустите Docker и проверьте `docker info`"
        ) from exc
    if status.returncode != 0:
        detail = (status.stderr or status.stdout).strip()
        raise ExecutorImageBuildError(
            "Docker найден, но daemon не запущен или недоступен. "
            "Запустите Docker и проверьте `docker info`." + (f" {detail}" if detail else "")
        )

    image = DEFAULT_CLAUDE_IMAGE if adapter == "claude-code" else DEFAULT_OPENCODE_IMAGE
    context = (
        _project_root() / "docker" / f"agent-{'claude' if adapter == 'claude-code' else 'opencode'}"
    )
    if not (context / "Dockerfile").is_file():
        raise ExecutorImageBuildError(f"не найден Dockerfile для {adapter}: {context}")

    process = subprocess.Popen(
        [docker, "build", "--progress=plain", "--tag", image, str(context)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    tail: list[str] = []
    try:
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            if line:
                tail.append(line)
                if on_progress is not None:
                    on_progress(line)
        return_code = process.wait(timeout=900)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise ExecutorImageBuildError(
            "сборка образа превысила 15 минут; проверьте сеть и Docker daemon"
        ) from exc
    if return_code != 0:
        detail = "\n".join(tail[-20:])
        raise ExecutorImageBuildError(detail or f"{docker} build завершился с кодом {return_code}")
    return image
