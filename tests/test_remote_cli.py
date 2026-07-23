"""Тесты thin CLI (ADR-0017 §3): RemoteClient против реального gateway-приложения.

Run-порождающие вызовы гоняются сервисом (TestClient обрывает фоновые задачи),
RemoteClient проверяется на всей read/decide/workspace-поверхности и стриме.
"""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from svarog_harness.cli import remote as remote_module
from svarog_harness.cli.remote import RemoteClient, RemoteError, load_remote_client
from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import ModelsConfig
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.runtime import orchestrator


class ScriptedProvider(ModelProvider):
    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        result = self.turns.pop(0)
        if on_text_delta is not None and result.content:
            on_text_delta(result.content)
        return result


def _write_config(ws: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "svarog.db"
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {db_path}\n",
        encoding="utf-8",
    )


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayService:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    return GatewayService(load_config(project_dir=ws), ws)


@pytest.fixture
def remote(service: GatewayService) -> RemoteClient:
    app = create_app(service, bearer_token="tok")
    return RemoteClient(
        base_url="http://testserver",
        token="tok",
        client_factory=lambda: TestClient(app, headers={"Authorization": "Bearer tok"}),
    )


def _patch_provider(monkeypatch: pytest.MonkeyPatch, turns: list[CompletionResult]) -> None:
    provider = ScriptedProvider(turns)

    def fake_default_provider(models_cfg: ModelsConfig, store: object = None) -> ModelProvider:
        return provider

    monkeypatch.setattr(orchestrator, "default_provider", fake_default_provider)


def _final(text: str = "готово") -> CompletionResult:
    return CompletionResult(content=text, usage=Usage(10, 5), finish_reason="stop")


async def _drain(service: GatewayService, run_id: str) -> None:
    async for event in service.stream(run_id):
        if event.get("type") == "run_finished":
            break


# --- RemoteClient против живого приложения ---------------------------------


async def test_remote_reads_and_stream(
    remote: RemoteClient, service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(monkeypatch, [_final("ответ агента")])
    run_id = await service.create_run("задача", None)
    await _drain(service, run_id)

    assert remote.whoami().tenant_id == "local"
    assert any(r.run_id == run_id for r in remote.list_runs())
    assert remote.get_run(run_id).state == "completed"
    diff = remote.diff(run_id)
    assert diff.committed == "" and diff.uncommitted == ""

    events = list(remote.stream_events(run_id))
    assert events[-1]["type"] == "run_finished"
    assert any(e.get("type") == "text" for e in events)


async def test_remote_approval_decision(
    remote: RemoteClient, service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(
        monkeypatch,
        [
            CompletionResult(
                content="",
                tool_calls=(
                    ToolCallRequest(
                        id="c1",
                        name="request_approval",
                        arguments_json='{"action": "шаг", "details": "д"}',
                    ),
                ),
                usage=Usage(10, 5),
            ),
            _final(),
        ],
    )
    run_id = await service.create_run("рискованная", None)
    await _drain(service, run_id)

    pending = remote.approvals()
    assert len(pending) == 1 and pending[0].run_id == run_id
    # decide через сервис (resume — фоновая задача, см. заголовок модуля);
    # remote.decide проверяем на 404-контракт.
    with pytest.raises(RemoteError, match="404"):
        remote.decide("deadbeef", approved=True, reason=None)


def test_remote_workspace_surface(remote: RemoteClient, service: GatewayService) -> None:
    remote.workspace_create("proj")
    with pytest.raises(RemoteError, match="409"):
        remote.workspace_create("proj")

    ws = service.workspace / "named" / "proj"
    (ws / "sub").mkdir()
    (ws / "sub" / "a.txt").write_text("данные", encoding="utf-8")

    listed = remote.workspaces()
    assert [w.name for w in listed] == ["proj"]

    # workspace_files остаётся нетипизированным: роут отдаёт либо JSON-листинг,
    # либо байты файла (response_model=None в gateway/api.py).
    listing = remote.workspace_files("proj", ".")
    assert [e["name"] for e in listing["entries"]] == ["sub"]
    content = remote.workspace_files("proj", "sub/a.txt")
    assert content == "данные".encode()

    archive = remote.workspace_archive("proj")
    assert archive[:2] == b"\x1f\x8b"  # gzip magic

    remote.workspace_rm("proj")
    assert remote.workspaces() == []
    with pytest.raises(RemoteError, match="404"):
        remote.workspace_rm("proj")


def test_remote_auth_error(service: GatewayService) -> None:
    app = create_app(service, bearer_token="tok")
    bad = RemoteClient(
        base_url="http://testserver",
        token="wrong",
        client_factory=lambda: TestClient(app, headers={"Authorization": "Bearer wrong"}),
    )
    with pytest.raises(RemoteError, match="401"):
        bad.whoami()


# --- профиль login ----------------------------------------------------------


def test_save_and_load_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = tmp_path / ".svarog" / "svarog.yaml"
    secrets_path = tmp_path / ".svarog" / "secrets.json"
    monkeypatch.setattr(remote_module, "USER_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(remote_module, "_USER_SECRETS_PATH", secrets_path)

    remote_module.save_remote_profile("https://svarog.example/", "sekret-tok")

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert data["remote"]["url"] == "https://svarog.example"
    assert data["remote"]["token_ref"] == "svarog_remote_token"
    # Токен — в SecretStore (0600), не в yaml.
    assert "sekret-tok" not in cfg_path.read_text(encoding="utf-8")
    stored = json.loads(secrets_path.read_text(encoding="utf-8"))
    assert stored["svarog_remote_token"] == "sekret-tok"
    assert (secrets_path.stat().st_mode & 0o777) == 0o600

    client = load_remote_client()
    assert client.base_url == "https://svarog.example"
    assert client.token == "sekret-tok"


def test_load_profile_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_module, "USER_CONFIG_PATH", tmp_path / "нет.yaml")
    with pytest.raises(RemoteError, match="svarog login"):
        load_remote_client()


def test_existing_user_config_survives_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """login дописывает секцию remote, не затирая остальной user-конфиг."""
    cfg_path = tmp_path / ".svarog" / "svarog.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("models:\n  default: local\n", encoding="utf-8")
    monkeypatch.setattr(remote_module, "USER_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(remote_module, "_USER_SECRETS_PATH", tmp_path / ".svarog" / "s.json")

    remote_module.save_remote_profile("https://h.example", None)

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert data["models"] == {"default": "local"}
    assert data["remote"]["url"] == "https://h.example"
