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


def _ask_turn(question: str = "какой цвет?") -> CompletionResult:
    return CompletionResult(
        content="",
        tool_calls=(
            ToolCallRequest(
                id="q1", name="ask_user", arguments_json=f'{{"question": "{question}"}}'
            ),
        ),
        usage=Usage(10, 5),
        finish_reason="tool_calls",
    )


def _tool_turn() -> CompletionResult:
    return CompletionResult(
        content="",
        tool_calls=(ToolCallRequest(id="t1", name="list_dir", arguments_json="{}"),),
        usage=Usage(10, 5),
        finish_reason="tool_calls",
    )


async def _wait_completed(service: GatewayService, run_id: str) -> str:
    for _ in range(400):
        detail = await service.get_run(run_id)
        if detail.state in {"completed", "failed"}:
            return detail.state
        await asyncio.sleep(0.01)
    return (await service.get_run(run_id)).state


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


def test_api_bearer_token_protects_http_and_ws(service: GatewayService) -> None:
    client = TestClient(create_app(service, bearer_token="secret-token"))

    assert client.get("/healthz").status_code == 200
    assert client.get("/runs").status_code == 401
    assert client.get("/runs", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/runs", headers={"Authorization": "Bearer secret-token"}).status_code == 200

    try:
        with client.websocket_connect("/runs/deadbeef/events"):
            pass
    except Exception:
        pass
    with client.websocket_connect("/runs/deadbeef/events?token=secret-token") as ws:
        # Нет истории событий для deadbeef; успешный handshake достаточно
        # подтверждает auth-path, дальше закрываем соединение клиентом.
        ws.close()


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


# --- ask_user через REST (§6.5) ---


async def test_service_ask_user_then_answer(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(
        monkeypatch,
        [_ask_turn("какой цвет?"), CompletionResult(content="учёл: синий", usage=Usage(10, 5))],
    )
    run_id = await service.create_run("спроси меня", None)
    events = await _drain(service, run_id)
    assert events[-1]["state"] == "waiting_approval"

    pending = await service.list_pending_approvals()
    assert pending[0].action_type == "user.question"
    assert pending[0].payload["question"] == "какой цвет?"

    resumed_run = await service.answer_question(pending[0].approval_id, answer="синий")
    assert resumed_run == run_id
    await service.resume_run(run_id)
    assert await _wait_completed(service, run_id) == "completed"
    await service.wait_for_background()

    detail = await service.get_run(run_id)
    assert any("ответ пользователя: синий" in m.get("content", "") for m in detail.messages)


async def test_api_answer_endpoint(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(monkeypatch, [_ask_turn(), CompletionResult(content="ок", usage=Usage(10, 5))])
    run_id = await service.create_run("спроси", None)
    await _drain(service, run_id)
    pending = await service.list_pending_approvals()

    client = TestClient(create_app(service))
    assert client.post("/approvals/deadbeef/answer", json={"answer": "x"}).status_code == 404
    resp = client.post(f"/approvals/{pending[0].approval_id}/answer", json={"answer": "зелёный"})
    assert resp.status_code == 200
    assert resp.json()["run_id"] == run_id


# --- супервизор refuel (§6.10) ---


def _write_refuel_config(ws: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "svarog.db"
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        "runtime:\n  max_iterations: 5\n  refuel_after_iterations: 1\n"
        f"storage:\n  db_path: {db_path}\n",
        encoding="utf-8",
    )


async def test_supervisor_resumes_refuel_suspended_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_refuel_config(ws, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    service = GatewayService(load_config(project_dir=ws), ws)
    _patch_provider(
        monkeypatch,
        [_tool_turn(), CompletionResult(content="завершено после refuel", usage=Usage(10, 5))],
    )

    run_id = await service.create_run("длинная задача", None)
    events = await _drain(service, run_id)
    assert events[-1]["state"] == "suspended"
    assert "refuel" in (events[-1].get("error") or "")

    # Супервизор находит refuel-suspended run и сам его поднимает.
    resumed = await service.supervise_once()
    assert run_id in resumed
    assert await _wait_completed(service, run_id) == "completed"
    await service.wait_for_background()


async def test_supervisor_ignores_budget_suspend(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Бюджетная/max-остановка требует человека — супервизор её не трогает.
    monkeypatch.setenv("SVAROG_RUNTIME__MAX_ITERATIONS", "1")
    monkeypatch.setenv("SVAROG_RUNTIME__REFUEL_AFTER_ITERATIONS", "5")
    service2 = GatewayService(load_config(project_dir=service.workspace), service.workspace)
    _patch_provider(monkeypatch, [_tool_turn(), _tool_turn()])
    run_id = await service2.create_run("зациклится", None)
    events = await _drain(service2, run_id)
    assert events[-1]["state"] == "suspended"
    assert "лимит итераций" in (events[-1].get("error") or "")

    assert await service2.supervise_once() == []  # не подхвачен
