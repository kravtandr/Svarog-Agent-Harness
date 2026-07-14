"""Интеграция ADR-0016 с реальным docker: полный run_once внешнего агента.

Проверяет живую топологию §2-§4: контейнер агента в internal-only сети,
relay-sidecar, bridge на хосте (LLM-прокси + MCP), agent-state volume,
launch-mounts (MCP-конфиг, hook, managed-настройки), трассировка стрима и
auto-commit. Агент — скрипт в workspace, печатающий claude-стрим и живьём
дергающий MCP-endpoint Svarog изнутри контейнера.
"""

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import AutonomyMode
from svarog_harness.runtime import orchestrator
from svarog_harness.runtime.agents.claude_code import ClaudeCodeAdapter
from svarog_harness.runtime.executor import AgentLaunch
from svarog_harness.runtime.orchestrator import RunHooks, TaskRunner
from svarog_harness.sandbox.docker import find_docker
from svarog_harness.storage.models import Message, Run, RunState

_DOCKER_AVAILABLE = find_docker() is not None

# Скрипт-агент исполняется ВНУТРИ контейнера (cwd=/workspace): живой вызов
# MCP-endpoint'а bridge через relay + создание файла + claude-стрим в stdout.
_FAKE_AGENT = """\
import json, os, pathlib, sys, urllib.request

def emit(obj):
    print(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()

emit({"type": "system", "subtype": "init", "session_id": "sess-docker-1"})

request = urllib.request.Request(
    os.environ["SVAROG_BRIDGE_URL"] + "/svarog/mcp",
    data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + os.environ["SVAROG_BRIDGE_TOKEN"],
    },
)
with urllib.request.urlopen(request, timeout=20) as response:
    tools = json.load(response)["result"]["tools"]

pathlib.Path("agent.txt").write_text("создано агентом в docker", encoding="utf-8")

emit({
    "type": "assistant",
    "message": {"role": "assistant", "content": [
        {"type": "text", "text": "mcp-ok:" + ",".join(sorted(t["name"] for t in tools))}
    ]},
    "session_id": "sess-docker-1",
})
emit({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "готово: agent.txt создан", "num_turns": 1,
    "total_cost_usd": 0.0, "usage": {"input_tokens": 10, "output_tokens": 5},
    "session_id": "sess-docker-1",
})
"""


class _DockerScriptAdapter(ClaudeCodeAdapter):
    """Команда — скрипт в workspace; парсер/capabilities/env — настоящие."""

    def command(self, launch: AgentLaunch) -> list[str]:
        return ["python3", "fake_agent.py"]


def _git(ws: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ws), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason="docker/podman недоступен")
async def test_external_run_once_in_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Docker Desktop находит сокет через ~/.docker (контексты): фиксируем
    # endpoint в DOCKER_HOST ДО подмены HOME, иначе CLI откатится на
    # несуществующий /var/run/docker.sock.
    docker = find_docker()
    assert docker is not None
    context = subprocess.run(
        [docker, "context", "inspect", "--format", '{{(index .Endpoints "docker").Host}}'],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if context:
        monkeypatch.setenv("DOCKER_HOST", context)
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    db_path = tmp_path / "state" / "svarog.db"
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: docker\n  image: python:3.12-slim\n"
        f"storage:\n  db_path: {db_path}\n"
        "executor:\n"
        "  type: external\n"
        "  external:\n"
        "    image: python:3.12-slim\n"
        "    enforcement: cooperative\n",
        encoding="utf-8",
    )
    (ws / "fake_agent.py").write_text(_FAKE_AGENT, encoding="utf-8")
    (ws / "README.md").write_text("проект\n", encoding="utf-8")
    _git(ws, "init", "-b", "main")
    _git(ws, "config", "user.email", "t@t")
    _git(ws, "config", "user.name", "t")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "init")
    monkeypatch.setattr(orchestrator, "adapter_for", lambda cfg: _DockerScriptAdapter())

    runner = TaskRunner(load_config(project_dir=ws), ws)
    outcome = await runner.run_once("сделай agent.txt", AutonomyMode.YOLO, hooks=RunHooks())

    assert outcome.state is RunState.COMPLETED, outcome.error
    assert outcome.final_answer == "готово: agent.txt создан"
    # Файл создан агентом внутри контейнера и виден на хосте (workspace rw).
    assert (ws / "agent.txt").read_text(encoding="utf-8") == "создано агентом в docker"

    async def fetch(db: AsyncSession) -> tuple[Run, list[Message]]:
        run = (await db.execute(select(Run))).scalars().one()
        messages = list(
            (await db.execute(select(Message).order_by(Message.index_in_run))).scalars()
        )
        return run, messages

    run, messages = await runner.with_db(fetch)
    assert run.meta["executor"] == "external"
    assert run.meta["agent_session_id"] == "sess-docker-1"
    # Агент живьём достучался до MCP-сервера Svarog через relay (§4):
    # в списке — человеческие гейты (память/скиллы не настроены в этом ws).
    mcp_text = next(
        str(m.content.get("content", "")) for m in messages if "mcp-ok:" in str(m.content)
    )
    assert "ask_user" in mcp_text
    assert "request_approval" in mcp_text
    assert "create_skill_proposal" in mcp_text
    # Auto-commit подхватил работу агента (Flow C).
    assert "agent.txt" in _git(ws, "show", "--stat", "HEAD")

    # Инфраструктура run'а подчищена: ни контейнеров, ни сетей svarog.
    leftovers = subprocess.run(
        [docker, "ps", "-a", "--filter", "label=svarog-agent=1", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    networks = subprocess.run(
        [docker, "network", "ls", "--filter", "label=svarog-agent=1", "--format", "{{.Name}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert leftovers == ""
    assert networks == ""
