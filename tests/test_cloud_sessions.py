"""Тесты серверной части Фазы 2 ADR-0017: sessions, cancel, whoami, NDJSON-стрим."""

import asyncio
import json
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
    """Проигрывает заготовленные turns и запоминает входящие messages."""

    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)
        self.seen_messages: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.seen_messages.append(list(messages))
        result = self.turns.pop(0)
        if on_text_delta is not None and result.content:
            on_text_delta(result.content)
        return result


class GatedProvider(ScriptedProvider):
    """Как ScriptedProvider, но каждый turn ждёт разрешения (для гонок cancel)."""

    def __init__(self, turns: list[CompletionResult]) -> None:
        super().__init__(turns)
        self.gate = asyncio.Event()

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        await self.gate.wait()
        self.gate.clear()
        return await super().complete(messages, tools, on_text_delta=on_text_delta)


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


@pytest.fixture
def client(service: GatewayService) -> TestClient:
    return TestClient(create_app(service))


def _install_provider(monkeypatch: pytest.MonkeyPatch, provider: ModelProvider) -> None:
    def fake_default_provider(models_cfg: ModelsConfig, store: object = None) -> ModelProvider:
        return provider

    monkeypatch.setattr(orchestrator, "default_provider", fake_default_provider)


def _final(text: str = "готово") -> CompletionResult:
    return CompletionResult(content=text, usage=Usage(10, 5), finish_reason="stop")


def _approval_turn() -> CompletionResult:
    return CompletionResult(
        content="",
        tool_calls=(
            ToolCallRequest(
                id="c1",
                name="request_approval",
                arguments_json='{"action": "рискованный шаг", "details": "деплой"}',
            ),
        ),
        usage=Usage(10, 5),
    )


async def _wait_state(service: GatewayService, run_id: str, target: set[str]) -> str:
    for _ in range(600):
        state = (await service.get_run(run_id)).state
        if state in target:
            return state
        await asyncio.sleep(0.01)
    return (await service.get_run(run_id)).state


# --- whoami ---------------------------------------------------------------


def test_whoami(client: TestClient) -> None:
    body = client.get("/whoami").json()
    assert body["tenant_id"] == "local"
    assert body["role"] == "superuser"
    assert body["active_runs"] == 0


# --- cancel ---------------------------------------------------------------


async def test_cancel_waiting_approval_run(
    client: TestClient, service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_provider(monkeypatch, ScriptedProvider([_approval_turn(), _final()]))
    run_id = await service.create_run("рискованная", None)
    assert await _wait_state(service, run_id, {"waiting_approval"}) == "waiting_approval"
    assert len(await service.list_pending_approvals()) == 1

    resp = client.post(f"/runs/{run_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["state"] == "cancelled"
    assert (await service.get_run(run_id)).state == "cancelled"
    # Pending-approval закрыт отказом — inbox чист.
    assert await service.list_pending_approvals() == []
    # Повторный cancel терминального run'а — 409.
    assert client.post(f"/runs/{run_id}/cancel").status_code == 409


async def test_cancel_running_cooperative(
    client: TestClient, service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Живой run: флаг ставится, loop отменяет на следующей границе итерации.

    Хронология: итерация 1 блокируется в LLM-вызове (gate закрыт) → cancel
    ставит флаг → gate открывается, модель отвечает tool-call'ом → tool
    исполняется → верх итерации 2 видит флаг → CANCELLED (не COMPLETED).
    """
    tool_turn = CompletionResult(
        content="",
        tool_calls=(
            ToolCallRequest(
                id="c1",
                name="write_file",
                arguments_json='{"path": "step.txt", "content": "шаг"}',
            ),
        ),
        usage=Usage(10, 5),
    )
    provider = GatedProvider([tool_turn, _final("не должно дойти")])
    _install_provider(monkeypatch, provider)
    run_id = await service.create_run("долгая", None)
    assert (await service.get_run(run_id)).state == "running"

    resp = client.post(f"/runs/{run_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["state"] == "cancelling"

    provider.gate.set()  # LLM возвращает tool-call; граница итерации 2 — cancel
    assert await _wait_state(service, run_id, {"cancelled"}) == "cancelled"
    # Второй turn не понадобился: run отменён до следующего LLM-вызова.
    assert len(provider.turns) == 1


def test_cancel_unknown_run(client: TestClient) -> None:
    assert client.post("/runs/nope/cancel").status_code == 404


# --- сессии ---------------------------------------------------------------


async def test_session_chat_flow(
    client: TestClient, service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    await service.create_workspace("proj")
    provider = ScriptedProvider([_final("ответ раз"), _final("ответ два")])
    _install_provider(monkeypatch, provider)

    created = client.post("/sessions", json={"title": "чат", "workspace": "proj"})
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    named = (service.workspace / "named" / "proj").resolve()
    assert created.json()["workspace"] == str(named)

    # Сообщения — через сервис: TestClient обрывает фоновые задачи после
    # ответа (см. комментарий в test_gateway), HTTP-слой проверен выше.
    run1 = await service.send_message(session_id, "привет", None)
    assert await _wait_state(service, run1, {"completed"}) == "completed"

    run2 = await service.send_message(session_id, "продолжи", None)
    assert await _wait_state(service, run2, {"completed"}) == "completed"

    # Второй run видит историю первого (§10.1): в prompt есть прошлая пара.
    joined = " ".join(m.content or "" for m in provider.seen_messages[-1])
    assert "привет" in joined and "ответ раз" in joined

    view = client.get(f"/sessions/{session_id}").json()
    assert [r["run_id"] for r in view["runs"]] == [run1, run2]
    assert view["workspace"] == str(named)

    # Оба run'а исполнялись в workspace сессии.
    detail = await service.get_run(run2)
    assert detail.state == "completed"


def test_session_unknown_workspace_404(client: TestClient) -> None:
    assert client.post("/sessions", json={"title": "x", "workspace": "ghost"}).status_code == 404
    assert client.post("/sessions/nope/messages", json={"text": "y"}).status_code == 404
    assert client.get("/sessions/nope").status_code == 404


def test_session_mutex_sources_422(client: TestClient) -> None:
    resp = client.post(
        "/sessions",
        json={"workspace": "a", "repo": {"url": "https://h/r.git"}},
    )
    assert resp.status_code == 422


# --- NDJSON-стрим ---------------------------------------------------------


async def test_events_ndjson_stream(
    client: TestClient, service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_provider(monkeypatch, ScriptedProvider([_final("стримовый ответ")]))
    run_id = await service.create_run("задача", None)
    assert await _wait_state(service, run_id, {"completed"}) == "completed"

    with client.stream("GET", f"/runs/{run_id}/events/stream") as resp:
        assert resp.status_code == 200
        events = [json.loads(line) for line in resp.iter_lines() if line]
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["state"] == "completed"
    assert any(e.get("type") == "text" for e in events)
