"""Reaper осиротевших sandbox-ресурсов (ADR-0016 §2, регрессия orphan-контейнер)."""

import os
import subprocess

import pytest

from svarog_harness.sandbox import reaper
from svarog_harness.sandbox.docker import find_docker

_DOCKER_AVAILABLE = find_docker() is not None


def test_owner_labels_carry_pid_and_boot() -> None:
    args = reaper.owner_label_args()
    joined = " ".join(args)
    assert "svarog-agent=1" in joined
    assert f"svarog-owner-pid={os.getpid()}" in joined
    # boot-метка присутствует всегда (значение может быть пустым на macOS).
    assert reaper._BOOT_LABEL in joined


def test_orphan_when_owner_pid_dead() -> None:
    # PID 999999999 почти наверняка не существует → ресурс осиротел.
    labels = {reaper._PID_LABEL: "999999999", reaper._BOOT_LABEL: reaper._boot_token()}
    assert reaper._is_orphan(labels) is True


def test_not_orphan_when_owner_alive() -> None:
    # Собственный PID жив → живой конкурентный run НЕ подметается.
    labels = {reaper._PID_LABEL: str(os.getpid()), reaper._BOOT_LABEL: reaper._boot_token()}
    assert reaper._is_orphan(labels) is False


def test_not_orphan_without_owner_label() -> None:
    # Нет метки владельца — судить нельзя, не трогаем (консервативно).
    assert reaper._is_orphan({"svarog-agent": "1"}) is False


def test_orphan_across_boots() -> None:
    # Метка из другой загрузки хоста → PID-пространство иное → orphan,
    # даже если такой PID сейчас случайно жив.
    labels = {reaper._PID_LABEL: str(os.getpid()), reaper._BOOT_LABEL: "boot-from-a-past-life"}
    current = reaper._boot_token()
    expected = current != "" and current != "boot-from-a-past-life"
    assert reaper._is_orphan(labels) is expected


def test_orphan_bad_pid_value() -> None:
    assert reaper._is_orphan({reaper._PID_LABEL: "not-an-int"}) is False


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason="docker/podman недоступен")
async def test_reaper_removes_dead_keeps_live_docker() -> None:
    """Живой docker: dead-owner контейнер+сеть снимаются, live-owner выживает."""
    docker = find_docker()
    assert docker is not None
    boot = reaper._boot_token()
    dead = "svarog-reaptest-dead"
    live = "svarog-reaptest-live"
    net = "svarog-reaptest-net"
    base = ["--label", "svarog-agent=1", "--label", f"{reaper._BOOT_LABEL}={boot}"]

    def run(argv: list[str]) -> None:
        subprocess.run([docker, *argv], check=True, capture_output=True)

    def exists(name: str, *, network: bool = False) -> bool:
        cmd = ["network", "ls"] if network else ["ps", "-a"]
        fmt = "{{.Name}}" if network else "{{.Names}}"
        out = subprocess.run(
            [docker, *cmd, "--filter", "label=svarog-agent=1", "--format", fmt],
            capture_output=True,
            text=True,
        ).stdout
        return name in out.split()

    try:
        run(
            [
                "run",
                "-d",
                "--name",
                dead,
                *base,
                "--label",
                f"{reaper._PID_LABEL}=999999999",
                "python:3.12-slim",
                "sleep",
                "infinity",
            ]
        )
        run(
            [
                "run",
                "-d",
                "--name",
                live,
                *base,
                "--label",
                f"{reaper._PID_LABEL}={os.getpid()}",
                "python:3.12-slim",
                "sleep",
                "infinity",
            ]
        )
        run(["network", "create", *base, "--label", f"{reaper._PID_LABEL}=999999999", net])

        reaped = await reaper.reap_orphaned_agents(docker)

        assert reaped == 2
        assert not exists(dead)
        assert exists(live)
        assert not exists(net, network=True)
    finally:
        for name in (dead, live):
            subprocess.run([docker, "rm", "-f", name], capture_output=True)
        subprocess.run([docker, "network", "rm", net], capture_output=True)
