"""Internal-сеть и relay-sidecar для внешнего агента (ADR-0016 §2/§3).

Топология: контейнер агента живёт в internal-only docker-сети (без default
route и DNS наружу). Единственный сосед — relay-sidecar, подключённый и к
internal-сети, и к bridge-сети с egress. Relay — тупой TCP-форвардер на
bridge-сервер Svarog на хосте: агент может достучаться ТОЛЬКО до
endpoint'ов bridge (LLM-прокси, MCP, hook), никакого произвольного egress.

Кроссплатформенность: адрес хоста — host.docker.internal; на Linux он
объявляется через `--add-host=…:host-gateway` (Docker 20.10+ / Podman 4+),
на Docker Desktop резолвится встроенно.
"""

import asyncio
import uuid
from dataclasses import dataclass

from svarog_harness.sandbox.base import SandboxError
from svarog_harness.sandbox.docker import find_docker
from svarog_harness.sandbox.reaper import owner_label_args

_DOCKER_TIMEOUT_SEC = 120.0
# Порт relay внутри internal-сети (фиксированный: агент получает URL вида
# http://<relay-host>:8080).
RELAY_PORT = 8080
_HOST_ALIAS = "host.docker.internal"

# Скрипт relay: stdlib-форвардер, живёт в официальном python-образе,
# передаётся аргументом -c (без mounts и без собственного образа).
_RELAY_SCRIPT = """
import asyncio, sys

HOST, PORT, LISTEN = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])

async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass

async def handle(client_reader, client_writer):
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(HOST, PORT)
    except OSError:
        client_writer.close()
        return
    await asyncio.gather(
        pipe(client_reader, upstream_writer),
        pipe(upstream_reader, client_writer),
        return_exceptions=True,
    )

async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", LISTEN)
    async with server:
        await server.serve_forever()

asyncio.run(main())
"""


@dataclass
class AgentNetwork:
    """Жизненный цикл internal-сети + relay одного run."""

    relay_image: str
    bridge_port: int
    suffix: str = ""
    _docker: str | None = None
    _network: str | None = None
    _relay_id: str | None = None
    _relay_name: str = ""

    async def start(self) -> None:
        self._docker = find_docker()
        if self._docker is None:
            raise SandboxError("docker/podman не найден: internal-сеть агента недоступна")
        suffix = self.suffix or uuid.uuid4().hex[:8]
        self._network = f"svarog-agent-{suffix}"
        self._relay_name = f"svarog-relay-{suffix}"
        code, _, err = await self._run(
            ["network", "create", "--internal", *owner_label_args(), self._network]
        )
        if code != 0:
            raise SandboxError(f"не удалось создать internal-сеть агента: {err.strip()}")
        code, out, err = await self._run(
            [
                "run",
                "-d",
                "--rm",
                "--name",
                self._relay_name,
                *owner_label_args(),
                "--network",
                self._network,
                # Linux: host-gateway объявляет адрес хоста; Docker Desktop
                # резолвит host.docker.internal сам (лишний alias безвреден).
                "--add-host",
                f"{_HOST_ALIAS}:host-gateway",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "64",
                "--memory",
                "128m",
                self.relay_image,
                "python",
                "-c",
                _RELAY_SCRIPT,
                _HOST_ALIAS,
                str(self.bridge_port),
                str(RELAY_PORT),
            ]
        )
        if code != 0:
            await self.stop()
            raise SandboxError(f"не удалось запустить relay-sidecar: {err.strip()}")
        self._relay_id = out.strip()
        # Egress relay к хосту — через вторую сеть (bridge с NAT). Агент до
        # bridge-сети не дотягивается: он подключён только к internal.
        code, _, err = await self._run(["network", "connect", "bridge", self._relay_name])
        if code != 0:
            await self.stop()
            raise SandboxError(f"relay не подключился к egress-сети: {err.strip()}")

    @property
    def network(self) -> str:
        assert self._network is not None, "AgentNetwork не запущена"
        return self._network

    def agent_base_url(self) -> str:
        """URL bridge для процессов ВНУТРИ internal-сети (DNS-имя relay)."""
        return f"http://{self._relay_name}:{RELAY_PORT}"

    async def stop(self) -> None:
        if self._docker is None:
            return
        if self._relay_id is not None or self._relay_name:
            await self._run(["rm", "-f", self._relay_name])
            self._relay_id = None
        if self._network is not None:
            await self._run(["network", "rm", self._network])
            self._network = None

    async def _run(self, argv: list[str]) -> tuple[int, str, str]:
        assert self._docker is not None
        proc = await asyncio.create_subprocess_exec(
            self._docker,
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=_DOCKER_TIMEOUT_SEC)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", "docker-команда превысила timeout"
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
