"""Тесты веб-доработок gateway: список сессий, лента, статика (план 2026-07-27)."""

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app
from svarog_harness.gateway.service import _RunHolder
from svarog_harness.runtime.summaries import short_arg, short_result


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


@pytest.mark.asyncio
async def test_list_sessions_newest_first(service: GatewayService) -> None:
    first = await service.create_session(title="старая")
    second = await service.create_session(title="свежая")

    listed = await service.list_sessions()

    assert [s.title for s in listed] == ["свежая", "старая"]
    assert [s.session_id for s in listed] == [second.session_id, first.session_id]
    assert listed[0].runs_count == 0
    assert listed[0].last_state is None
    assert isinstance(listed[0].updated_at, datetime)


@pytest.mark.asyncio
async def test_list_sessions_endpoint(service: GatewayService) -> None:
    await service.create_session(title="через API")
    client = TestClient(create_app(service=service))

    response = client.get("/sessions")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["title"] == "через API"
    assert body[0]["runs_count"] == 0


def test_short_arg_prefers_meaningful_key() -> None:
    assert short_arg({"path": "memory/index.py", "content": "x" * 500}) == "memory/index.py"
    assert short_arg({"command": "uv run pytest -q"}) == "uv run pytest -q"
    assert short_arg({"query": "стоп-слова префикс"}) == "стоп-слова префикс"
    assert short_arg({}) == ""


def test_short_arg_truncates_long_values() -> None:
    assert short_arg({"path": "a" * 200}) == "a" * 119 + "…"


def test_short_result_takes_first_line_of_output() -> None:
    assert (
        short_result(ok=True, output="записано 1234 символов в memory/index.py")
        == "записано 1234 символов в memory/index.py"
    )
    hits = "- a.md — фрагмент\n- b.md — фрагмент"
    assert short_result(ok=True, output=hits) == "- a.md — фрагмент"
    assert short_result(ok=True, output="\n\n  первая непустая  \nвторая") == "первая непустая"


def test_short_result_reports_failure_and_truncates() -> None:
    assert short_result(ok=False, output="", error="exit code 1: no such file") == (
        "exit code 1: no such file"
    )
    assert short_result(ok=False, output="", error=None) == "ошибка"
    assert short_result(ok=True, output="") == ""
    assert short_result(ok=True, output="я" * 100) == "я" * 59 + "…"


@pytest.mark.asyncio
async def test_tool_events_carry_arg_and_result(service: GatewayService) -> None:
    published: list[dict[str, object]] = []
    service.events.publish = lambda run_id, event: published.append(event)  # type: ignore[method-assign]

    started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    holder = _RunHolder()
    holder.run_id = "run-1"
    hooks = service._event_hooks(holder, started)

    assert hooks.on_tool_call is not None
    assert hooks.on_tool_result is not None
    hooks.on_tool_call("write_file", {"path": "memory/index.py", "content": "x"})
    hooks.on_tool_result("write_file", "succeeded", "+58 −4")

    assert published == [
        {"type": "tool_call", "tool": "write_file", "arg": "memory/index.py"},
        {"type": "tool_result", "tool": "write_file", "status": "succeeded", "result": "+58 −4"},
    ]


@pytest.mark.asyncio
async def test_session_thread_replays_user_calls_and_answer(service: GatewayService) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from svarog_harness.storage.models import Run, RunState, ToolCall, ToolCallStatus

    session = await service.create_session(title="лента")

    async def seed(db: AsyncSession) -> None:
        db.add(
            Run(
                id="run-1",
                session_id=session.session_id,
                task="Добавь FTS-поиск",
                state=RunState.COMPLETED,
                autonomy="supervised",
            )
        )
        db.add(
            ToolCall(
                id="tc-1",
                run_id="run-1",
                tool_name="write_file",
                arguments={"path": "memory/index.py"},
                result={"output": "записано 1234 символов в memory/index.py"},
                status=ToolCallStatus.SUCCEEDED,
            )
        )
        await db.commit()

    await service._read(seed)

    thread = await service.session_thread(session.session_id)

    assert thread.items[0].kind == "user"
    assert thread.items[0].text == "Добавь FTS-поиск"
    call = next(item for item in thread.items if item.kind == "call")
    assert call.name == "write_file"
    assert call.server is None
    assert call.arg == "memory/index.py"
    assert call.result == "записано 1234 символов в memory/index.py"
    assert call.status == "succeeded"


@pytest.mark.asyncio
async def test_session_thread_splits_mcp_server_from_tool_name(service: GatewayService) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from svarog_harness.storage.models import Run, RunState, ToolCall, ToolCallStatus

    session = await service.create_session(title="mcp")

    async def seed(db: AsyncSession) -> None:
        db.add(
            Run(
                id="run-2",
                session_id=session.session_id,
                task="Посмотри задачи",
                state=RunState.COMPLETED,
                autonomy="supervised",
            )
        )
        db.add(
            ToolCall(
                id="tc-2",
                run_id="run-2",
                tool_name="github/list_issues",
                arguments={"query": "label: memory"},
                result={"output": "2 задачи"},
                status=ToolCallStatus.SUCCEEDED,
            )
        )
        await db.commit()

    await service._read(seed)

    thread = await service.session_thread(session.session_id)

    call = next(item for item in thread.items if item.kind == "call")
    assert call.server == "github"
    assert call.name == "list_issues"


def test_session_thread_endpoint_404(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))
    assert client.get("/sessions/нет-такой/messages").status_code == 404


def test_root_explains_when_bundle_missing(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Пустой каталог: ни упакованного бандла, ни сборки в чекауте.
    monkeypatch.setenv("SVAROG_WEB_DIST", str(tmp_path / "нет-сборки"))
    client = TestClient(create_app(service=service))

    response = client.get("/")

    assert response.status_code == 404
    assert "npm --prefix web run build" in response.json()["detail"]


def test_root_serves_bundle_when_present(
    service: GatewayService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Сварог</title>", encoding="utf-8")
    monkeypatch.setenv("SVAROG_WEB_DIST", str(dist))

    client = TestClient(create_app(service=service))
    response = client.get("/")

    assert response.status_code == 200
    assert "Сварог" in response.text


def test_cors_disabled_by_default(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))
    response = client.get("/healthz", headers={"Origin": "http://localhost:5173"})
    assert "access-control-allow-origin" not in response.headers


def test_cors_enabled_by_env(service: GatewayService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVAROG_GATEWAY__CORS_ORIGINS", "http://localhost:5173")
    client = TestClient(create_app(service=service))

    response = client.get("/healthz", headers={"Origin": "http://localhost:5173"})

    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


@pytest.mark.asyncio
async def test_approval_event_published(service: GatewayService) -> None:
    from svarog_harness.storage.models import Approval

    published: list[dict[str, object]] = []
    service.events.publish = lambda run_id, event: published.append(event)  # type: ignore[method-assign]

    started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    holder = _RunHolder()
    holder.run_id = "run-1"
    hooks = service._event_hooks(holder, started)

    approval = Approval(
        id="ap-1",
        run_id="run-1",
        action_type="run_shell",
        payload={"command": "uv run pytest -q"},
    )
    assert hooks.on_approval_requested is not None
    hooks.on_approval_requested(approval)

    assert published == [
        {
            "type": "approval_required",
            "approval_id": "ap-1",
            "action_type": "run_shell",
            "payload": {"command": "uv run pytest -q"},
        }
    ]
