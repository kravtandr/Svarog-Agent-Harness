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
from svarog_harness.runtime import run_assembly


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

    monkeypatch.setattr(run_assembly, "default_provider", fake_default_provider)


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


# --- сессии с внешним executor'ом (ADR-0016 × ADR-0017) --------------------


def _write_external_config(ws: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "svarog.db"
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: docker\n"
        "executor:\n"
        "  type: external\n"
        "  external:\n"
        "    adapter: claude-code\n"
        "    image: agent:test\n"
        "    api_key_ref: PROVIDER_KEY\n"
        # Тёплый sandbox выключен: эти тесты мокают run_once/executor и не
        # поднимают реальную инфраструктуру (bridge требует секрет/докер).
        "cloud:\n  warm_session_ttl_sec: 0\n"
        f"storage:\n  db_path: {db_path}\n",
        encoding="utf-8",
    )


class _FakeInfra:
    network_name = None
    extra_mounts = None

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


async def test_run_once_external_continues_agent_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_once с session_id у внешнего executor'а: не отказ, а продолжение
    сессии агента — executor получает session_id и agent_session_id
    предыдущего run'а Session (как CLI-chat, ADR-0016 фаза 3)."""
    from svarog_harness.config.schema import AutonomyMode
    from svarog_harness.runtime.loop import RunOutcome
    from svarog_harness.runtime.orchestrator import RunHooks, TaskRunner
    from svarog_harness.sandbox.local import LocalEnvironment
    from svarog_harness.storage.models import RunState
    from svarog_harness.trace.recorder import TraceRecorder

    ws = tmp_path / "ws"
    ws.mkdir()
    _write_external_config(ws, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = TaskRunner(load_config(project_dir=ws), ws)

    # Docker/bridge не поднимаем: интеграция executor↔container покрыта
    # test_external_executor/test_external_docker, здесь — шов session-прокидки.
    monkeypatch.setattr(runner, "assert_sandbox_available", lambda: None)
    monkeypatch.setattr(runner, "assert_external_autonomy_supported", lambda a: None)
    monkeypatch.setattr(runner, "build_agent_infra", _FakeInfra)
    monkeypatch.setattr(runner, "prepare_agent_launch", lambda infra: None)
    monkeypatch.setattr(runner, "build_environment", lambda infra=None: LocalEnvironment(ws))
    monkeypatch.setattr(runner, "wire_bridge_control", lambda *a, **k: None)

    captured: dict[str, object] = {}

    def fake_build_executor(
        recorder: TraceRecorder, env: object, hooks: object, **kwargs: object
    ) -> object:
        class _FakeExecutor:
            async def run(
                self,
                task: str,
                autonomy: AutonomyMode,
                *,
                session_id: str | None = None,
                parent_run_id: str | None = None,
                agent_session: str | None = None,
            ) -> RunOutcome:
                captured["session_id"] = session_id
                captured["agent_session"] = agent_session
                run = await recorder.start_run(
                    task=task,
                    autonomy=autonomy.value,
                    model="agent",
                    session_id=session_id,
                    workspace=str(ws),
                )
                await recorder.finish_run(run, RunState.COMPLETED)
                return RunOutcome(run.id, RunState.COMPLETED, "ок", 1, 0, 0.0)

        return _FakeExecutor()

    monkeypatch.setattr(runner, "build_external_executor", fake_build_executor)

    # Предыстория: сессия и завершённый run внешнего агента с agent_session_id.
    async def seed(db: object) -> str:
        recorder = TraceRecorder(db)  # type: ignore[arg-type]
        session = await recorder.create_session(title="чат")
        prev = await recorder.start_run(
            task="первое", autonomy="yolo", model="agent", session_id=session.id
        )
        await recorder.merge_run_meta(prev, {"agent_session_id": "agent-abc"})
        await recorder.finish_run(prev, RunState.COMPLETED)
        return session.id

    session_id = await runner.with_db(seed)

    outcome = await runner.run_once(
        "продолжи", AutonomyMode.YOLO, hooks=RunHooks(), session_id=session_id
    )
    assert outcome.state is RunState.COMPLETED
    assert captured == {"session_id": session_id, "agent_session": "agent-abc"}


async def test_send_message_external_skips_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """send_message при внешнем executor'е не собирает history (контекст —
    в сессии агента), но передаёт session_id в run_once."""
    from types import SimpleNamespace

    from svarog_harness.runtime.loop import RunOutcome
    from svarog_harness.runtime.orchestrator import TaskRunner
    from svarog_harness.storage.models import RunState
    from svarog_harness.trace.recorder import TraceRecorder

    ws = tmp_path / "ws"
    ws.mkdir()
    _write_external_config(ws, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    service = GatewayService(load_config(project_dir=ws), ws)

    captured: dict[str, object] = {}

    async def fake_run_once(
        self: TaskRunner,
        task: str,
        autonomy: object,
        *,
        hooks: object,
        session_id: str | None = None,
        history: object = None,
        resources: object = None,
    ) -> RunOutcome:
        captured["session_id"] = session_id
        captured["history"] = history

        async def action(db: object) -> str:
            recorder = TraceRecorder(db)  # type: ignore[arg-type]
            run = await recorder.start_run(
                task=task,
                autonomy=str(getattr(autonomy, "value", autonomy)),
                model="agent",
                session_id=session_id,
                workspace=str(self._workspace),
            )
            await recorder.finish_run(run, RunState.COMPLETED)
            return run.id

        run_id = await self.with_db(action)
        hooks.on_run_started(SimpleNamespace(id=run_id))  # type: ignore[attr-defined]
        return RunOutcome(run_id, RunState.COMPLETED, "ок", 1, 0, 0.0)

    monkeypatch.setattr(TaskRunner, "run_once", fake_run_once)

    view = await service.create_session(title="чат")
    run_id = await service.send_message(view.session_id, "привет", None)
    for _ in range(200):
        if (await service.get_run(run_id)).state == "completed":
            break
        await asyncio.sleep(0.01)

    assert captured["session_id"] == view.session_id
    assert captured["history"] is None  # history не собирается для external


# --- тёплый sandbox сессии (ADR-0017) --------------------------------------


async def test_warm_session_reuses_environment(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Два сообщения сессии — одна подготовка sandbox (prepare один раз)."""
    from svarog_harness.runtime.orchestrator import TaskRunner

    calls = {"prepare": 0}
    original = TaskRunner.prepare_session_resources

    async def counting_prepare(self: TaskRunner, autonomy: object) -> object:
        calls["prepare"] += 1
        return await original(self, autonomy)  # type: ignore[arg-type]

    monkeypatch.setattr(TaskRunner, "prepare_session_resources", counting_prepare)
    await service.create_workspace("proj")
    _install_provider(monkeypatch, ScriptedProvider([_final("раз"), _final("два")]))

    view = await service.create_session(title="чат", workspace_name="proj")
    run1 = await service.send_message(view.session_id, "привет", None)
    assert await _wait_state(service, run1, {"completed"}) == "completed"
    run2 = await service.send_message(view.session_id, "ещё", None)
    assert await _wait_state(service, run2, {"completed"}) == "completed"

    assert calls["prepare"] == 1  # sandbox поднят один раз на всю серию
    assert view.session_id in service._warm
    await service.close_warm_sessions()
    assert service._warm == {}


async def test_warm_session_ttl_sweep(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Слот, простоявший дольше TTL, закрывается idle-GC; свежий — остаётся."""
    _install_provider(monkeypatch, ScriptedProvider([_final()]))
    view = await service.create_session(title="чат")
    run_id = await service.send_message(view.session_id, "привет", None)
    assert await _wait_state(service, run_id, {"completed"}) == "completed"

    slot = service._warm[view.session_id]
    closed = {"n": 0}
    original_close = slot.resources.close

    async def spy_close() -> None:
        closed["n"] += 1
        await original_close()

    monkeypatch.setattr(slot.resources, "close", spy_close)

    await service._sweep_warm_sessions()
    assert view.session_id in service._warm  # свежий слот не тронут

    slot.last_used -= float(service.cfg.cloud.warm_session_ttl_sec) + 1
    await service._sweep_warm_sessions()
    assert view.session_id not in service._warm
    assert closed["n"] == 1


async def test_warm_slot_dropped_on_run_failure(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Упавшая нога сбрасывает тёплый слот — следующее сообщение поднимет свежий."""
    from svarog_harness.runtime.orchestrator import TaskRunner

    async def broken_run_once(self: TaskRunner, *args: object, **kwargs: object) -> object:
        raise RuntimeError("контейнер умер")

    monkeypatch.setattr(TaskRunner, "run_once", broken_run_once)
    view = await service.create_session(title="чат")
    with pytest.raises(RuntimeError, match="контейнер умер"):
        await service.send_message(view.session_id, "привет", None)
    assert view.session_id not in service._warm


async def test_delete_workspace_closes_warm_slot(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Удаление named workspace закрывает тёплые sandbox'ы его сессий."""
    _install_provider(monkeypatch, ScriptedProvider([_final()]))
    await service.create_workspace("proj")
    view = await service.create_session(title="чат", workspace_name="proj")
    run_id = await service.send_message(view.session_id, "привет", None)
    assert await _wait_state(service, run_id, {"completed"}) == "completed"
    assert view.session_id in service._warm

    await service.delete_workspace("proj")
    assert view.session_id not in service._warm
    assert not (service.workspace / "named" / "proj").exists()


async def test_warm_disabled_by_ttl_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """warm_session_ttl_sec=0 — прежнее поведение: sandbox на каждый run."""
    from svarog_harness.runtime.orchestrator import TaskRunner

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
        "sandbox:\n  type: local-trusted\n"
        "cloud:\n  warm_session_ttl_sec: 0\n"
        f"storage:\n  db_path: {db_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    service = GatewayService(load_config(project_dir=ws), ws)

    calls = {"prepare": 0}
    original = TaskRunner.prepare_session_resources

    async def counting_prepare(self: TaskRunner, autonomy: object) -> object:
        calls["prepare"] += 1
        return await original(self, autonomy)  # type: ignore[arg-type]

    monkeypatch.setattr(TaskRunner, "prepare_session_resources", counting_prepare)
    _install_provider(monkeypatch, ScriptedProvider([_final()]))

    view = await service.create_session(title="чат")
    run_id = await service.send_message(view.session_id, "привет", None)
    assert await _wait_state(service, run_id, {"completed"}) == "completed"
    assert calls["prepare"] == 0
    assert service._warm == {}
