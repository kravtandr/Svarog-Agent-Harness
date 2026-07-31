"""Настройки 31.07.2026: провайдеры, executor-дефолты, вкладка MCP."""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayService:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: docker\n"
        f"secrets:\n  path: {tmp_path / 'secrets.json'}\n"
        "executor:\n"
        "  type: external\n"
        "  external:\n"
        "    adapter: opencode\n"
        "    image: svarog/agent-opencode:latest\n"
        "    base_url: http://localhost:9\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    return GatewayService(load_config(project_dir=ws), ws)


@pytest.fixture
def client(service: GatewayService) -> TestClient:
    return TestClient(create_app(service))


def test_add_provider_writes_config_and_secret(
    client: TestClient, service: GatewayService, tmp_path: Path
) -> None:
    resp = client.post(
        "/models/providers",
        json={
            "name": "groq",
            "base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b",
            "api_key": "sk-секрет",
        },
    )
    assert resp.status_code == 200, resp.text
    data = yaml.safe_load(service.config_path.read_text(encoding="utf-8"))
    groq = data["models"]["providers"]["groq"]
    assert groq["base_url"] == "https://api.groq.com/openai/v1"
    assert groq["model"] == "llama-3.3-70b"
    assert groq["api_key_ref"] == "GROQ_API_KEY"
    # Ключ — в SecretStore, не в yaml (ADR-0006).
    assert "sk-секрет" not in service.config_path.read_text(encoding="utf-8")
    secrets = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert secrets["GROQ_API_KEY"] == "sk-секрет"
    # Конфиг перечитан: провайдер виден в /models.
    names = [p["name"] for p in client.get("/models").json()]
    assert "groq" in names


def test_add_provider_rejects_bad_input(client: TestClient) -> None:
    assert (
        client.post(
            "/models/providers",
            json={"name": "плохое имя", "base_url": "https://x", "model": "m"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/models/providers",
            json={"name": "ok", "base_url": "ftp://x", "model": "m"},
        ).status_code
        == 422
    )


def test_executor_defaults_native_switches_provider_and_model(
    client: TestClient, service: GatewayService
) -> None:
    client.post(
        "/models/providers",
        json={"name": "groq", "base_url": "https://api.groq.com/openai/v1", "model": "ll"},
    )
    resp = client.post(
        "/executors/defaults",
        json={"executor": "native", "provider": "groq", "model": "llama-3.3-70b"},
    )
    assert resp.status_code == 200, resp.text
    data = yaml.safe_load(service.config_path.read_text(encoding="utf-8"))
    assert data["models"]["default"] == "groq"
    assert data["models"]["providers"]["groq"]["model"] == "llama-3.3-70b"


def test_executor_defaults_opencode_derives_provider(
    client: TestClient, service: GatewayService
) -> None:
    resp = client.post(
        "/executors/defaults",
        json={"executor": "opencode", "provider": "local", "model": "z-ai/glm-5.2"},
    )
    assert resp.status_code == 200, resp.text
    data = yaml.safe_load(service.config_path.read_text(encoding="utf-8"))
    ext = data["executor"]["external"]
    assert ext["model"] == "z-ai/glm-5.2"
    assert ext["base_url"] == "http://localhost:9"  # /v1 срезан из карточки
    assert ext["auth"] == "api-key"


def test_executor_defaults_claude_model_only(client: TestClient, service: GatewayService) -> None:
    resp = client.post("/executors/defaults", json={"executor": "claude-code", "model": "opus"})
    assert resp.status_code == 200, resp.text
    data = yaml.safe_load(service.config_path.read_text(encoding="utf-8"))
    assert data["executor"]["external"]["model"] == "opus"
    # Провайдер для claude-code — отказ: у него своя подписка.
    assert (
        client.post(
            "/executors/defaults", json={"executor": "claude-code", "provider": "local"}
        ).status_code
        == 422
    )


def test_mcp_add_list_remove(client: TestClient, service: GatewayService) -> None:
    resp = client.post(
        "/mcp",
        json={
            "name": "fetch",
            "command": "uvx",
            "args": ["mcp-server-fetch"],
            "risk": "medium",
        },
    )
    assert resp.status_code == 200, resp.text
    listed = client.get("/mcp").json()
    assert [s["name"] for s in listed] == ["fetch"]
    assert listed[0]["command"] == "uvx"
    assert listed[0]["args"] == ["mcp-server-fetch"]
    assert listed[0]["risk"] == "medium"

    assert client.delete("/mcp/fetch").status_code == 200
    assert client.get("/mcp").json() == []
    assert client.delete("/mcp/fetch").status_code == 404


def test_mcp_test_reports_failure_honestly(client: TestClient) -> None:
    resp = client.post("/mcp/test", json={"command": "no-such-binary-абв", "args": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]


async def _noop() -> AsyncIterator[None]:  # pragma: no cover — заглушка типов
    yield
