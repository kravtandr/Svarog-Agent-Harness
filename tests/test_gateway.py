"""Тесты REST/WebSocket gateway (#24): GatewayService + FastAPI-приложение."""

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
    cfg = load_config(project_dir=ws)
    return GatewayService(cfg, ws)


def _patch_provider(monkeypatch: pytest.MonkeyPatch, turns: list[CompletionResult]) -> None:
    provider = ScriptedProvider(turns)

    def fake_default_provider(models_cfg: ModelsConfig, store: object = None) -> ModelProvider:
        return provider

    monkeypatch.setattr(orchestrator, "default_provider", fake_default_provider)


async def _drain(service: GatewayService, run_id: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    async for event in service.stream(run_id):
        events.append(event)
        if event.get("type") == "run_finished":
            break
    return events


async def test_service_runs_task_and_streams_events(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(
        monkeypatch,
        [
            CompletionResult(
                content="",
                tool_calls=(
                    ToolCallRequest(
                        id="c1",
                        name="write_file",
                        arguments_json='{"path": "out.txt", "content": "готово"}',
                    ),
                ),
                usage=Usage(10, 5),
            ),
            CompletionResult(content="Файл создан", usage=Usage(10, 5), finish_reason="stop"),
        ],
    )
    run_id = await service.create_run("создай файл", None)
    events = await _drain(service, run_id)

    finished = events[-1]
    assert finished["type"] == "run_finished"
    assert finished["state"] == "completed"
    assert {e["type"] for e in events} >= {"tool_call", "run_finished"}
    assert (service.workspace / "out.txt").read_text(encoding="utf-8") == "готово"

    detail = await service.get_run(run_id)
    assert detail.state == "completed"
    assert any(tc.tool_name == "write_file" for tc in detail.tool_calls)


async def test_service_approval_then_resume(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
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
                        arguments_json='{"action": "рискованный шаг", "details": "rm -rf build"}',
                    ),
                ),
                usage=Usage(10, 5),
            ),
            CompletionResult(content="одобрено, продолжаю", usage=Usage(10, 5)),
        ],
    )
    run_id = await service.create_run("рискованная", None)
    events = await _drain(service, run_id)
    assert events[-1]["state"] == "waiting_approval"

    pending = await service.list_pending_approvals()
    assert len(pending) == 1
    assert pending[0].run_id == run_id

    resumed_run = await service.decide_approval(pending[0].approval_id, approved=True, reason=None)
    assert resumed_run == run_id
    await service.resume_run(run_id)

    # Дождаться завершения возобновлённой ноги (свежий event-лог после reset).
    for _ in range(200):
        detail = await service.get_run(run_id)
        if detail.state == "completed":
            break
        await asyncio.sleep(0.01)
    assert detail.state == "completed"


async def test_api_endpoints_and_run_lifecycle(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Прогоняем run сервисом до завершения (персист в trace + история событий),
    # затем читаем его через HTTP. Живой фоновой прогон под uvicorn проверяется
    # отдельно; TestClient обрывает fire-and-forget задачи после ответа.
    _patch_provider(
        monkeypatch,
        [CompletionResult(content="Готово", usage=Usage(10, 5), finish_reason="stop")],
    )
    (service.workspace / "skills" / "note").mkdir(parents=True)
    (service.workspace / "skills" / "note" / "SKILL.md").write_text(
        "---\nname: note\ndescription: Заметка.\nversion: 0.1.0\nrisk: low\n---\n# note\n",
        encoding="utf-8",
    )
    run_id = await service.create_run("простая", None)
    await _drain(service, run_id)

    client = TestClient(create_app(service))

    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/runs/deadbeef").status_code == 404
    assert client.post("/approvals/deadbeef", json={"approved": True}).status_code == 404

    skills = client.get("/skills").json()
    assert [s["name"] for s in skills] == ["note"]

    detail = client.get(f"/runs/{run_id}").json()
    assert detail["state"] == "completed"
    assert detail["run_id"] == run_id

    listed = client.get("/runs").json()
    assert any(r["run_id"] == run_id for r in listed)

    # WS реплеит историю завершённого run'а и закрывается на run_finished.
    with client.websocket_connect(f"/runs/{run_id}/events") as ws:
        finished = None
        while True:
            event = ws.receive_json()
            if event["type"] == "run_finished":
                finished = event
                break
    assert finished is not None
    assert finished["state"] == "completed"


def test_api_create_run_returns_id(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(
        monkeypatch,
        [CompletionResult(content="Готово", usage=Usage(10, 5), finish_reason="stop")],
    )
    client = TestClient(create_app(service))
    created = client.post("/runs", json={"task": "простая"})
    assert created.status_code == 201
    assert created.json()["run_id"]
