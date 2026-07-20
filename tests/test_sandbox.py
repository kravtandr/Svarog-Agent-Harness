"""Тесты sandbox: local backend, аргументы docker run, выбор backend'а.

Интеграционные docker-тесты гоняются только при доступном docker
(skipif) — юнит-слой (run_args, factory) от docker не зависит.
"""

import os
from pathlib import Path

import pytest

from svarog_harness.config.schema import SandboxConfig
from svarog_harness.sandbox import (
    DockerEnvironment,
    LocalEnvironment,
    SandboxError,
    create_environment,
    find_docker,
)

_DOCKER_AVAILABLE = find_docker() is not None


async def test_local_execute_and_cwd(tmp_path: Path) -> None:
    env = LocalEnvironment(tmp_path)
    result = await env.execute("pwd", timeout_sec=10)
    assert result.exit_code == 0
    assert result.stdout.strip() == str(tmp_path)
    assert not result.timed_out


async def test_local_timeout(tmp_path: Path) -> None:
    env = LocalEnvironment(tmp_path)
    result = await env.execute("sleep 30", timeout_sec=0.2)
    assert result.timed_out
    assert result.exit_code == 124


def test_factory_selects_backend(tmp_path: Path) -> None:
    local = create_environment(SandboxConfig(type="local-trusted"), tmp_path)
    assert isinstance(local, LocalEnvironment)
    docker = create_environment(SandboxConfig(type="docker"), tmp_path)
    assert isinstance(docker, DockerEnvironment)


def test_docker_run_args_enforce_layer1(tmp_path: Path) -> None:
    """Инварианты слоя 1 (ADR-0002) присутствуют в аргументах docker run."""
    skills = tmp_path / "skills"
    skills.mkdir()
    env = DockerEnvironment(tmp_path, SandboxConfig(), skills_dir=skills)
    args = env.run_args()

    def value_of(flag: str) -> str:
        return args[args.index(flag) + 1]

    assert value_of("--network") == "none"
    assert value_of("--cap-drop") == "ALL"
    assert value_of("--security-opt") == "no-new-privileges"
    assert value_of("--pids-limit") == "256"
    assert value_of("--memory") == "8g"
    assert value_of("--cpus") == "4"
    assert f"{tmp_path}:/workspace:rw" in args
    assert f"{skills}:/skills:ro" in args
    assert value_of("--user") == f"{os.getuid()}:{os.getgid()}"
    # Метки владельца для reaper осиротевших контейнеров (ADR-0016 §2).
    assert "svarog-agent=1" in args
    assert f"svarog-owner-pid={os.getpid()}" in args
    # Команда контейнера: образ + sleep infinity.
    assert args[-3:] == [SandboxConfig().image, "sleep", "infinity"]


def test_docker_run_args_without_skills(tmp_path: Path) -> None:
    args = DockerEnvironment(tmp_path, SandboxConfig()).run_args()
    assert not any(":/skills:ro" in a for a in args)


async def test_docker_execute_before_start_raises(tmp_path: Path) -> None:
    env = DockerEnvironment(tmp_path, SandboxConfig())
    with pytest.raises(SandboxError, match="не запущен"):
        await env.execute("echo x", timeout_sec=5)


async def test_docker_missing_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("SVAROG_DOCKER_BINARY", raising=False)
    env = DockerEnvironment(tmp_path, SandboxConfig())
    with pytest.raises(SandboxError, match="local-trusted"):
        await env.start()


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason="docker/podman недоступен")
class TestDockerIntegration:
    """Полный цикл против реального docker: сеть off, workspace rw, timeout."""

    async def test_full_cycle(self, tmp_path: Path) -> None:
        (tmp_path / "input.txt").write_text("данные", encoding="utf-8")
        env = DockerEnvironment(tmp_path, SandboxConfig())
        await env.start()
        try:
            result = await env.execute("cat input.txt", timeout_sec=30)
            assert result.exit_code == 0
            assert "данные" in result.stdout

            # workspace rw: файл появляется на хосте
            result = await env.execute("echo из-контейнера > out.txt", timeout_sec=30)
            assert result.exit_code == 0
            assert (tmp_path / "out.txt").read_text(encoding="utf-8").strip() == "из-контейнера"

            # слой 1: сети нет (у python-образа есть python, но нет curl — проверяем через /dev/tcp)
            result = await env.execute(
                "timeout 5 bash -c 'echo > /dev/tcp/1.1.1.1/80' && echo REACHED || echo BLOCKED",
                timeout_sec=30,
            )
            assert "BLOCKED" in result.stdout

            # timeout внутри контейнера
            result = await env.execute("sleep 30", timeout_sec=1)
            assert result.timed_out
        finally:
            await env.cleanup()

    async def test_cleanup_removes_container(self, tmp_path: Path) -> None:
        env = DockerEnvironment(tmp_path, SandboxConfig())
        await env.start()
        container_id = env._container_id
        assert container_id
        await env.cleanup()
        check = LocalEnvironment(tmp_path)
        result = await check.execute(
            f"docker ps -a --no-trunc --format '{{{{.ID}}}}' | grep -c {container_id} || true",
            timeout_sec=30,
        )
        assert result.stdout.strip() == "0"


async def test_local_stream_handles_lines_longer_than_reader_limit(tmp_path: Path) -> None:
    # Регрессия: readline() падал ValueError («Separator is found, but chunk is
    # longer than limit») на строках > 64КБ — JSON-события внешнего агента.
    env = LocalEnvironment(tmp_path)
    lines: list[str] = []

    async def on_line(line: str) -> None:
        lines.append(line)

    result = await env.stream(
        "python3 -c \"print('x' * 300000); print('done')\"",
        timeout_sec=30,
        on_line=on_line,
    )
    assert result.exit_code == 0
    assert lines[0] == "x" * 300000
    assert lines[1] == "done"


async def test_read_line_unbounded_eof_and_split() -> None:
    import asyncio

    from svarog_harness.sandbox.base import read_line_unbounded

    reader = asyncio.StreamReader(limit=16)  # крошечный лимит: форсируем переполнение
    reader.feed_data(b"a" * 100 + b"\nshort\ntail-no-newline")
    reader.feed_eof()
    assert await read_line_unbounded(reader) == b"a" * 100 + b"\n"
    assert await read_line_unbounded(reader) == b"short\n"
    assert await read_line_unbounded(reader) == b"tail-no-newline"
    assert await read_line_unbounded(reader) == b""  # EOF
