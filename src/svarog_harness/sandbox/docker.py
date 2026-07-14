"""Docker sandbox backend (§6.9, ADR-0002 — слой 1 enforcement).

Инварианты слоя 1, которые держатся независимо от решений Policy Engine:
сеть выключена (`--network none`), non-root user, CPU/RAM/PID limits,
файловая система ограничена явными mounts (workspace rw, skills ro),
секреты и git-credentials в контейнер не передаются.

Адаптировано из hermes-agent `tools/environments/docker.py` (MIT,
NousResearch): поиск docker/podman-бинаря, security-флаги (cap-drop ALL,
no-new-privileges, pids-limit, tmpfs), long-lived контейнер `sleep infinity`
с исполнением через `docker exec`. Опущены: session snapshot, cwd-маркеры,
переиспользование контейнеров между процессами — контейнер живет в рамках
одного run (ADR-0005).
"""

import asyncio
import contextlib
import os
import shutil
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from svarog_harness.config.schema import SandboxConfig
from svarog_harness.sandbox.base import (
    ExecResult,
    ExecutionEnvironment,
    SandboxError,
    read_stream_tail,
)

# Явный override пути к docker/podman (аналог HERMES_DOCKER_BINARY).
_BINARY_ENV_OVERRIDE = "SVAROG_DOCKER_BINARY"

# Время на `docker run`, включая возможный pull образа.
_START_TIMEOUT_SEC = 300.0
# Запас поверх внутреннего coreutils `timeout` — на случай зависшего exec-клиента.
_CLIENT_TIMEOUT_MARGIN_SEC = 10.0


def find_docker() -> str | None:
    """Найти docker или podman; None — контейнерный runtime недоступен."""
    override = os.environ.get(_BINARY_ENV_OVERRIDE)
    if override:
        path = Path(override)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    for name in ("docker", "podman"):
        found = shutil.which(name)
        if found:
            return found
    return None


class DockerEnvironment(ExecutionEnvironment):
    """Hardened-контейнер на время run: `docker run -d … sleep infinity`.

    Каждая команда — отдельный `docker exec … bash -c`, стартующий в
    /workspace (bind mount workspace хоста). Timeout команды обеспечивает
    coreutils `timeout` внутри контейнера (exit code 124); внешний
    wait_for с запасом лишь защищает от зависшего docker-клиента.
    """

    def __init__(
        self,
        workspace: Path,
        cfg: SandboxConfig,
        *,
        skills_dir: Path | None = None,
        env: dict[str, str] | None = None,
        network: str | None = None,
        extra_mounts: list[tuple[Path, str, bool]] | None = None,
    ) -> None:
        self.workspace = workspace
        self._cfg = cfg
        self._skills_dir = skills_dir
        # Явно выданные секреты в окружение контейнера (ADR-0006); слой 1 не даёт
        # им утечь по сети (--network none) — контейнер изолирован.
        self._env = env or {}
        # ADR-0016 §2: для внешнего агента вместо "none" — internal-only сеть,
        # где единственный сосед — relay к bridge-серверу Svarog.
        self._network = network or "none"
        # (host_path, container_path, read_only): agent-state volume (§5),
        # managed policy и hook-скрипт (§6) — только явные mounts.
        self._extra_mounts = extra_mounts or []
        self._name = f"svarog-{uuid.uuid4().hex[:12]}"
        self._docker: str | None = None
        self._container_id: str | None = None

    def run_args(self) -> list[str]:
        """Аргументы `docker run` (без самого бинаря) — юнит-тестируемы без docker."""
        args = [
            "run",
            "-d",
            "--name",
            self._name,
            "--label",
            "svarog-agent=1",
            # Слой 1 (ADR-0002): по умолчанию сеть выключена; для внешнего
            # агента (ADR-0016 §2) — internal-only сеть с relay к bridge.
            "--network",
            self._network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            self._cfg.memory_limit,
            "--cpus",
            str(self._cfg.cpu_limit),
            "--tmpfs",
            "/tmp:rw,nosuid,size=512m",
            # У non-root uid нет записи в /etc/passwd образа — HOME задаем явно.
            "-e",
            "HOME=/tmp/home",
            "-v",
            f"{self.workspace}:/workspace:rw",
            "-w",
            "/workspace",
        ]
        for key in sorted(self._env):
            args += ["-e", f"{key}={self._env[key]}"]
        user = _host_user_spec()
        if user is not None:
            args += ["--user", user]
        if self._skills_dir is not None:
            args += ["-v", f"{self._skills_dir}:/skills:ro"]
        for host_path, container_path, read_only in self._extra_mounts:
            suffix = ":ro" if read_only else ":rw"
            args += ["-v", f"{host_path}:{container_path}{suffix}"]
        args += [self._cfg.image, "sleep", "infinity"]
        return args

    async def start(self) -> None:
        self._docker = find_docker()
        if self._docker is None:
            raise SandboxError(
                "docker/podman не найден: установите Docker или переключите "
                "sandbox.type в 'local-trusted' (режим без изоляции, §17)"
            )
        exit_code, stdout, stderr = await self._run_docker(
            self.run_args(), timeout_sec=_START_TIMEOUT_SEC
        )
        if exit_code != 0:
            raise SandboxError(
                f"не удалось запустить sandbox-контейнер (образ {self._cfg.image}): "
                f"{stderr.strip() or stdout.strip()}"
            )
        self._container_id = stdout.strip()

    async def execute(self, command: str, *, timeout_sec: float) -> ExecResult:
        if self._container_id is None or self._docker is None:
            raise SandboxError("sandbox-контейнер не запущен: сначала вызовите start()")
        inner_timeout = max(1, int(timeout_sec))
        argv = [
            "exec",
            self._container_id,
            "timeout",
            "--kill-after=5",
            str(inner_timeout),
            "bash",
            "-c",
            command,
        ]
        exit_code, stdout, stderr = await self._run_docker(
            argv, timeout_sec=timeout_sec + _CLIENT_TIMEOUT_MARGIN_SEC
        )
        if exit_code is None:
            return ExecResult(exit_code=124, stdout="", stderr="", timed_out=True)
        # 124 — coreutils timeout убил команду; 137 оставляем как обычный exit code
        # (SIGKILL может быть и OOM-killer'ом — не выдаем его за timeout).
        return ExecResult(
            exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=exit_code == 124
        )

    async def stream(
        self,
        command: str,
        *,
        timeout_sec: float,
        on_line: Callable[[str], Awaitable[None]],
    ) -> ExecResult:
        if self._container_id is None or self._docker is None:
            raise SandboxError("sandbox-контейнер не запущен: сначала вызовите start()")
        inner_timeout = max(1, int(timeout_sec))
        proc = await asyncio.create_subprocess_exec(
            self._docker,
            "exec",
            self._container_id,
            "timeout",
            "--kill-after=5",
            str(inner_timeout),
            "bash",
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None and proc.stderr is not None
        stderr_task = asyncio.create_task(read_stream_tail(proc.stderr))
        try:
            # Запас поверх внутреннего coreutils timeout — от зависшего клиента.
            async with asyncio.timeout(timeout_sec + _CLIENT_TIMEOUT_MARGIN_SEC):
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    await on_line(raw.decode(errors="replace").rstrip("\n"))
                await proc.wait()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            return ExecResult(exit_code=124, stdout="", stderr="", timed_out=True)
        except asyncio.CancelledError:
            # Suspend (ADR-0016 §7): docker-exec клиент убивается сразу;
            # процесс в контейнере добьёт cleanup (`rm -f`) в finally run'а.
            proc.kill()
            await proc.wait()
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            raise
        exit_code = proc.returncode or 0
        return ExecResult(
            exit_code=exit_code,
            stdout="",
            stderr=await stderr_task,
            timed_out=exit_code == 124,  # coreutils timeout внутри контейнера
        )

    async def cleanup(self) -> None:
        if self._container_id is None or self._docker is None:
            return
        await self._run_docker(["rm", "-f", self._container_id], timeout_sec=30.0)
        self._container_id = None

    async def _run_docker(
        self, argv: list[str], *, timeout_sec: float
    ) -> tuple[int | None, str, str]:
        """Выполнить docker-команду; exit_code=None — клиент убит по timeout."""
        assert self._docker is not None
        proc = await asyncio.create_subprocess_exec(
            self._docker,
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_sec
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return None, "", ""
        return (
            proc.returncode or 0,
            stdout_bytes.decode(errors="replace"),
            stderr_bytes.decode(errors="replace"),
        )


def _host_user_spec() -> str | None:
    """`uid:gid` текущего пользователя — non-root в контейнере и владелец
    файлов в bind-mounted workspace; None на платформах без POSIX uid."""
    get_uid = getattr(os, "getuid", None)
    get_gid = getattr(os, "getgid", None)
    if get_uid is None or get_gid is None:
        return None
    return f"{get_uid()}:{get_gid()}"
