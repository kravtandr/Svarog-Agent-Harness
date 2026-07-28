"""Слэш-команды и подсказки файлов для поля ввода (план 2026-07-28)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app
from svarog_harness.gateway.commands import WEB_COMMANDS


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


def test_registry_has_six_web_commands() -> None:
    names = [cmd.name for cmd in WEB_COMMANDS]
    assert names == ["help", "new", "sessions", "executor", "policies", "copy"]
    assert all(cmd.help for cmd in WEB_COMMANDS), "у каждой команды есть описание"


def test_commands_endpoint(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))
    body = client.get("/commands").json()
    assert [c["name"] for c in body] == [cmd.name for cmd in WEB_COMMANDS]
    assert body[0]["usage"].startswith("/")
