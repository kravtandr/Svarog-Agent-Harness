"""Тесты bridge-сервера и control-plane (ADR-0016 §3/§4/§6/§7):
LLM-прокси (инжекция ключа, метеринг, бюджет), MCP-сервер, hook-мост,
grace → suspend, decision cache, инфраструктура run'а."""

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.schema import (
    AutonomyMode,
    ExternalExecutorConfig,
    PoliciesConfig,
    RuntimeConfig,
)
from svarog_harness.llm.openai_compatible import ApiKeyError
from svarog_harness.policy.engine import PolicyEngine
from svarog_harness.policy.rules import PolicyRule
from svarog_harness.runtime.agent_infra import ExternalAgentInfra
from svarog_harness.runtime.agents.claude_code import ClaudeCodeAdapter
from svarog_harness.runtime.bridge import (
    BridgeBudget,
    QuietHTTPServer,
    RunBridge,
    UpstreamConfig,
    _UsageMeter,
)
from svarog_harness.runtime.bridge_control import (
    FINGERPRINT_KEY,
    BridgeControl,
    call_fingerprint,
)
from svarog_harness.secrets.store import EnvSecretStore
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import Approval, ApprovalStatus, MemoryChange
from svarog_harness.trace.recorder import TraceRecorder

# --- Фейковый upstream (провайдер) ------------------------------------------

_ANTHROPIC_JSON = {
    "id": "msg_1",
    "type": "message",
    "content": [{"type": "text", "text": "ok"}],
    "usage": {"input_tokens": 100, "output_tokens": 25},
}
_SSE_BODY = (
    'event: message_start\ndata: {"type":"message_start","message":{"usage":'
    '{"input_tokens":50,"output_tokens":1}}}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":30}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


class _Upstream:
    """Фейковый провайдер: пишет увиденные заголовки, отдаёт JSON или SSE."""

    def __init__(self) -> None:
        self.seen_headers: list[dict[str, str]] = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                upstream.seen_headers.append({k.lower(): v for k, v in self.headers.items()})
                length = int(self.headers.get("content-length") or 0)
                self.rfile.read(length)
                if self.path.endswith("/sse"):
                    body = _SSE_BODY.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    body = json.dumps(_ANTHROPIC_JSON).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        self.server = QuietHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream() -> Any:
    server = _Upstream()
    yield server
    server.stop()


def _bridge(
    upstream_url: str,
    *,
    api_key: str | None = "real-provider-key",
    expected_bearer: str | None = None,
    max_tokens: int = 1_000_000,
    handlers: dict[str, Any] | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> RunBridge:
    return RunBridge(
        upstream=UpstreamConfig(
            base_url=upstream_url, api_key=api_key, expected_bearer=expected_bearer
        ),
        budget=BridgeBudget(max_tokens=max_tokens, max_cost_usd=100.0),
        loop=loop or asyncio.get_event_loop_policy().new_event_loop(),
        control_handlers=handlers or {},
    )


# --- LLM-прокси (§3) ---------------------------------------------------------


async def test_proxy_injects_key_and_meters_json(upstream: _Upstream) -> None:
    bridge = _bridge(upstream.url, loop=asyncio.get_running_loop())
    bridge.start()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{bridge.local_url()}/v1/messages",
                headers={"x-api-key": bridge.token},
                json={"model": "m", "messages": []},
            )
        assert response.status_code == 200
        assert response.json()["usage"]["output_tokens"] == 25
        # Ключ провайдера подставлен host-side; per-run токен upstream не видел.
        seen = upstream.seen_headers[-1]
        assert seen["x-api-key"] == "real-provider-key"
        assert bridge.token not in json.dumps(upstream.seen_headers)
        assert bridge.usage.input_tokens == 100
        assert bridge.usage.output_tokens == 25
        assert bridge.usage.requests == 1
    finally:
        bridge.stop()


async def test_proxy_meters_sse_stream(upstream: _Upstream) -> None:
    bridge = _bridge(upstream.url, loop=asyncio.get_running_loop())
    bridge.start()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{bridge.local_url()}/v1/messages/sse",
                headers={"Authorization": f"Bearer {bridge.token}"},
                json={"stream": True},
            )
        assert response.status_code == 200
        assert "message_delta" in response.text  # стрим дошёл до клиента
        assert bridge.usage.input_tokens == 50
        assert bridge.usage.output_tokens == 30  # кумулятивный delta
    finally:
        bridge.stop()


async def test_proxy_requires_token(upstream: _Upstream) -> None:
    bridge = _bridge(upstream.url, loop=asyncio.get_running_loop())
    bridge.start()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{bridge.local_url()}/v1/messages", headers={"x-api-key": "wrong"}, json={}
            )
        assert response.status_code == 401
        assert not upstream.seen_headers  # до провайдера запрос не дошёл
    finally:
        bridge.stop()


async def test_proxy_budget_enforcement(upstream: _Upstream) -> None:
    bridge = _bridge(upstream.url, max_tokens=110, loop=asyncio.get_running_loop())
    bridge.start()
    try:
        async with httpx.AsyncClient() as client:
            first = await client.post(
                f"{bridge.local_url()}/v1/messages",
                headers={"x-api-key": bridge.token},
                json={},
            )
            second = await client.post(
                f"{bridge.local_url()}/v1/messages",
                headers={"x-api-key": bridge.token},
                json={},
            )
        assert first.status_code == 200  # 125 токенов — уже сверх 110
        assert second.status_code == 429  # enforcement, а не кооперация
        assert bridge.usage.budget_exceeded
    finally:
        bridge.stop()


async def test_subscription_passthrough_forwards_agent_auth(upstream: _Upstream) -> None:
    """subscription: OAuth-токен агента форвардится как есть, ключ не инжектится,
    usage считается, а bridge авторизует LLM-путь сверкой с токеном (§3)."""
    oauth = "sk-ant-oat01-fake-subscription-token"
    bridge = _bridge(
        upstream.url, api_key=None, expected_bearer=oauth, loop=asyncio.get_running_loop()
    )
    assert bridge.upstream.passthrough
    bridge.start()
    try:
        async with httpx.AsyncClient() as client:
            # Агент шлёт свой OAuth-токен (+ beta-заголовок, добавленный Claude Code).
            ok = await client.post(
                f"{bridge.local_url()}/v1/messages",
                headers={
                    "Authorization": f"Bearer {oauth}",
                    "anthropic-beta": "oauth-2025-04-20",
                },
                json={"model": "m", "messages": []},
            )
            # Неверный токен — 401, до провайдера не доходит.
            bad = await client.post(
                f"{bridge.local_url()}/v1/messages",
                headers={"Authorization": "Bearer wrong"},
                json={},
            )
        assert ok.status_code == 200
        assert bad.status_code == 401
        seen = upstream.seen_headers[-1]
        # OAuth-токен и beta-заголовок агента дошли до провайдера БЕЗ подмены;
        # ключ провайдера не инжектировался (его в subscription-режиме нет).
        assert seen["authorization"] == f"Bearer {oauth}"
        assert seen["anthropic-beta"] == "oauth-2025-04-20"
        assert "x-api-key" not in seen
        # Метеринг работает и в pass-through.
        assert bridge.usage.output_tokens == 25
        assert len(upstream.seen_headers) == 1  # bad-запрос до провайдера не дошёл
    finally:
        bridge.stop()


async def test_subscription_control_still_needs_run_token(upstream: _Upstream) -> None:
    """Control-endpoints требуют per-run токен даже в subscription-режиме."""
    oauth = "sk-ant-oat01-fake"

    async def echo(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    bridge = _bridge(
        upstream.url,
        api_key=None,
        expected_bearer=oauth,
        handlers={"echo": echo},
        loop=asyncio.get_running_loop(),
    )
    bridge.start()
    try:
        async with httpx.AsyncClient() as client:
            # OAuth-токен подписки НЕ даёт доступа к control-плоскости.
            with_oauth = await client.post(
                f"{bridge.local_url()}/svarog/echo",
                headers={"Authorization": f"Bearer {oauth}"},
                json={},
            )
            with_run_token = await client.post(
                f"{bridge.local_url()}/svarog/echo",
                headers={"Authorization": f"Bearer {bridge.token}"},
                json={},
            )
        assert with_oauth.status_code == 401
        assert with_run_token.json() == {"ok": True}
    finally:
        bridge.stop()


async def test_control_endpoint_roundtrip(upstream: _Upstream) -> None:
    async def echo(payload: dict[str, Any]) -> dict[str, Any]:
        return {"echo": payload.get("x")}

    bridge = _bridge(upstream.url, handlers={"echo": echo}, loop=asyncio.get_running_loop())
    bridge.start()
    try:
        async with httpx.AsyncClient() as client:
            ok = await client.post(
                f"{bridge.local_url()}/svarog/echo",
                headers={"x-api-key": bridge.token},
                json={"x": 42},
            )
            missing = await client.post(
                f"{bridge.local_url()}/svarog/nope",
                headers={"x-api-key": bridge.token},
                json={},
            )
        assert ok.json() == {"echo": 42}
        assert missing.status_code == 404
    finally:
        bridge.stop()


def test_usage_meter_openai_formats() -> None:
    chat = _UsageMeter("openai")
    chat.feed(json.dumps({"usage": {"prompt_tokens": 7, "completion_tokens": 3}}).encode())
    chat.finish()
    assert (chat.input_tokens, chat.output_tokens) == (7, 3)
    responses = _UsageMeter("openai")
    responses.feed(
        b'data: {"type":"response.completed","response":{"usage":'
        b'{"input_tokens":11,"output_tokens":5}}}\n\n'
    )
    responses.finish()
    assert (responses.input_tokens, responses.output_tokens) == (11, 5)


# --- BridgeControl: MCP-сервер и hook-мост (§4/§6/§7) ------------------------


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


def _db_action(tmp_path: Path) -> Callable[..., Awaitable[Any]]:
    path = tmp_path / "db" / "svarog.sqlite3"

    async def action(fn: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
        init_db(path)
        engine = create_engine(path)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return action


def _control(
    tmp_path: Path,
    *,
    autonomy: AutonomyMode = AutonomyMode.YOLO,
    rules: list[PolicyRule] | None = None,
    grace_sec: float = 0.05,
    memory_dir: Path | None = None,
    on_approval_prompt: Callable[[Approval], Awaitable[None]] | None = None,
) -> BridgeControl:
    policy = PolicyEngine(
        autonomy=autonomy,
        policies=PoliciesConfig(),
        workspace=tmp_path,
        rules=rules or [],
    )
    return BridgeControl(
        db_action=_db_action(tmp_path),
        policy=policy,
        memory_dir=memory_dir,
        skills=[],
        proposal_sink=[],
        approval_grace_sec=grace_sec,
        on_approval_prompt=on_approval_prompt,
    )


async def _start_run(db: AsyncSession, workspace: str) -> str:
    run = await TraceRecorder(db).start_run(
        task="t", autonomy="yolo", model="external:test", workspace=workspace
    )
    return run.id


async def test_mcp_initialize_and_tools_list(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    control = _control(tmp_path, memory_dir=mem)
    init = await control.handle_mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"]["name"] == "svarog"
    listed = await control.handle_mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert {
        "remember",
        "read_memory",
        "create_skill_proposal",
        "ask_user",
        "request_approval",
    } <= names


async def test_mcp_remember_enqueues_memory(db: AsyncSession, tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    (mem / "projects").mkdir(parents=True)
    control = _control(tmp_path, memory_dir=mem)
    run_id = await _start_run(db, str(tmp_path))
    control.run_id = run_id
    response = await control.handle_mcp(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "remember",
                "arguments": {
                    "file": "projects/demo.md",
                    "operation": "create",
                    "content": "# demo\n\nвнешний агент узнал факт\n",
                },
            },
        }
    )
    assert not response["result"]["isError"], response
    rows = list((await db.execute(select(MemoryChange))).scalars())
    assert len(rows) == 1
    assert rows[0].source_run_id == run_id


async def test_hook_allows_reads_in_yolo(tmp_path: Path) -> None:
    control = _control(tmp_path)
    decision = await control.handle_hook({"tool_name": "Read", "tool_input": {"file_path": "a.py"}})
    assert decision == {"decision": "allow", "reason": ""}


async def test_hook_denies_by_project_rule(tmp_path: Path) -> None:
    rule = PolicyRule(match="file.*", decision="deny", reason="infra руками не трогаем")
    control = _control(tmp_path, rules=[rule])
    decision = await control.handle_hook(
        {"tool_name": "Write", "tool_input": {"path": "infra/x.tf"}}
    )
    assert decision["decision"] == "deny"
    assert "infra" in decision["reason"]


async def test_hook_grace_timeout_requests_suspend(db: AsyncSession, tmp_path: Path) -> None:
    # bash-эвристика эскалирует опасную команду до HIGH → supervised требует
    # approval (общий конвейер policy, тот же что у нативного bash).
    control = _control(tmp_path, autonomy=AutonomyMode.SUPERVISED, grace_sec=0.05)
    control.run_id = await _start_run(db, str(tmp_path))
    dangerous = {"command": "rm -rf /"}
    decision = await control.handle_hook({"tool_name": "Bash", "tool_input": dangerous})
    assert decision["decision"] == "deny"
    assert "SVAROG-PENDING" in decision["reason"]
    assert control.suspend.is_set()
    # Approval создан и ждёт человека.
    approvals = list((await db.execute(select(Approval))).scalars())
    assert len(approvals) == 1
    assert approvals[0].status is ApprovalStatus.PENDING
    assert approvals[0].payload[FINGERPRINT_KEY] == call_fingerprint("Bash", dangerous)


async def test_hook_decision_cache_after_resume(db: AsyncSession, tmp_path: Path) -> None:
    """Approve по отпечатку (§7): ретрай после resume проходит без второго approval."""
    control = _control(tmp_path, autonomy=AutonomyMode.SUPERVISED, grace_sec=0.05)
    run_id = await _start_run(db, str(tmp_path))
    control.run_id = run_id
    recorder = TraceRecorder(db)
    run = await recorder.get_run(run_id)
    assert run is not None
    approved_call = {"command": "git push --force origin main"}
    approval = await recorder.create_approval(
        run,
        action_type="bash.exec",
        payload={FINGERPRINT_KEY: call_fingerprint("Bash", approved_call)},
    )
    await recorder.decide_approval(approval, approved=True, decided_by="tester")
    decision = await control.handle_hook({"tool_name": "Bash", "tool_input": approved_call})
    assert decision["decision"] == "allow"
    denied_call = {"command": "rm -rf /"}
    denied = await recorder.create_approval(
        run,
        action_type="bash.exec",
        payload={FINGERPRINT_KEY: call_fingerprint("Bash", denied_call)},
    )
    await recorder.decide_approval(denied, approved=False, decided_by="tester", reason="опасно")
    decision = await control.handle_hook({"tool_name": "Bash", "tool_input": denied_call})
    assert decision["decision"] == "deny"
    assert "опасно" in decision["reason"]


async def test_approval_prompt_resolves_gate_live(db: AsyncSession, tmp_path: Path) -> None:
    """Живой промпт (§7): колбэк решает approval во время grace — без suspend."""
    recorder = TraceRecorder(db)
    prompted: list[Approval] = []

    async def prompt(approval: Approval) -> None:
        prompted.append(approval)
        fresh = await recorder.find_approval_by_prefix(approval.id)
        await recorder.decide_approval(fresh, approved=True, decided_by="chat")

    control = _control(
        tmp_path, autonomy=AutonomyMode.SUPERVISED, grace_sec=5.0, on_approval_prompt=prompt
    )
    control.run_id = await _start_run(db, str(tmp_path))
    decision = await control.handle_hook(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}
    )
    assert decision["decision"] == "allow"
    assert not control.suspend.is_set()
    assert len(prompted) == 1
    assert prompted[0].action_type == "bash.exec"


async def test_approval_prompt_failure_falls_back_to_grace(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Сбой промпта не роняет гейт: grace дорабатывает и просит suspend."""

    async def broken_prompt(_: Approval) -> None:
        raise RuntimeError("stdin закрыт")

    control = _control(
        tmp_path,
        autonomy=AutonomyMode.SUPERVISED,
        grace_sec=0.05,
        on_approval_prompt=broken_prompt,
    )
    control.run_id = await _start_run(db, str(tmp_path))
    decision = await control.handle_hook(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}
    )
    assert decision["decision"] == "deny"
    assert "SVAROG-PENDING" in decision["reason"]
    assert control.suspend.is_set()


async def test_ask_user_answered_within_grace(db: AsyncSession, tmp_path: Path) -> None:
    control = _control(tmp_path, grace_sec=5.0)
    run_id = await _start_run(db, str(tmp_path))
    control.run_id = run_id
    recorder = TraceRecorder(db)

    async def answer_soon() -> None:
        for _ in range(100):
            await asyncio.sleep(0.05)
            approvals = list((await db.execute(select(Approval))).scalars())
            if approvals:
                await recorder.answer_question(approvals[0], answer="зелёный", answered_by="tester")
                return

    answer_task = asyncio.create_task(answer_soon())
    response = await control.handle_mcp(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "ask_user", "arguments": {"question": "какой цвет?"}},
        }
    )
    await answer_task
    text = response["result"]["content"][0]["text"]
    assert not response["result"]["isError"]
    assert "зелёный" in text
    assert not control.suspend.is_set()


def test_call_fingerprint_canonical() -> None:
    a = call_fingerprint("Bash", {"command": "ls", "timeout": 5})
    b = call_fingerprint("Bash", {"timeout": 5, "command": "ls"})
    assert a == b
    assert a != call_fingerprint("Bash", {"command": "ls -la"})


# --- ExternalAgentInfra (§2-§5) ----------------------------------------------


async def test_infra_local_mode_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "sk-fake")
    cfg = ExternalExecutorConfig(
        image="img:1", api_key_ref="FAKE_PROVIDER_KEY", enforcement="cooperative"
    )
    infra = ExternalAgentInfra(
        cfg,
        RuntimeConfig(),
        ClaudeCodeAdapter(),
        EnvSecretStore(),
        state_root=tmp_path / ".svarog",
        docker_mode=False,
    )
    await infra.start()
    try:
        infra.prepare_launch("память проекта", "- skill: demo", cooperative=True)
        env = infra.agent_env()
        # Ключ провайдера в env агента НЕ попадает — только per-run токен.
        assert "sk-fake" not in json.dumps(env)
        assert env["ANTHROPIC_API_KEY"] == infra.bridge.token if infra.bridge else False
        assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
        # Клиентские таймауты человеческих гейтов — дольше grace (§7):
        # клиент не должен бросить вызов до suspend.
        assert int(env["SVAROG_HOOK_TIMEOUT"]) > cfg.approval_grace_sec
        assert int(env["MCP_TOOL_TIMEOUT"]) == int(env["SVAROG_HOOK_TIMEOUT"]) * 1000
        # Контекст — в state volume, launch-файлы записаны.
        claude_md = infra.state_dir / "CLAUDE.md"
        assert "память проекта" in claude_md.read_text(encoding="utf-8")
        assert infra.mcp_config_path is not None
        mcp_config = json.loads(Path(infra.mcp_config_path).read_text(encoding="utf-8"))
        assert "svarog" in mcp_config["mcpServers"]
        assert infra.settings_path is not None
        managed = json.loads(Path(infra.settings_path).read_text(encoding="utf-8"))
        assert "PreToolUse" in managed["hooks"]
    finally:
        await infra.stop()
    assert not Path(infra.mcp_config_path).exists()  # launch-файлы одноразовые


async def test_infra_subscription_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_OAUTH", "sk-ant-oat01-real-subscription")
    cfg = ExternalExecutorConfig(image="img:1", auth="subscription", oauth_token_ref="CLAUDE_OAUTH")
    infra = ExternalAgentInfra(
        cfg,
        RuntimeConfig(),
        ClaudeCodeAdapter(),
        EnvSecretStore(),
        state_root=tmp_path / ".svarog",
        docker_mode=False,
    )
    await infra.start()
    try:
        assert infra.bridge is not None
        # Прокси в pass-through: ключа провайдера нет, ожидаемый Bearer = токен.
        assert infra.bridge.upstream.passthrough
        assert infra.bridge.upstream.api_key is None
        assert infra.bridge.upstream.expected_bearer == "sk-ant-oat01-real-subscription"
        env = infra.agent_env()
        # Агент получает OAuth-токен подписки; ANTHROPIC_API_KEY НЕ ставится.
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-real-subscription"
        assert "ANTHROPIC_API_KEY" not in env
        assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    finally:
        await infra.stop()


async def test_infra_missing_api_key_fail_closed(tmp_path: Path) -> None:
    cfg = ExternalExecutorConfig(image="img:1", api_key_ref="NO_SUCH_KEY_REF")
    infra = ExternalAgentInfra(
        cfg,
        RuntimeConfig(),
        ClaudeCodeAdapter(),
        EnvSecretStore(),
        state_root=tmp_path / ".svarog",
        docker_mode=False,
    )
    with pytest.raises(ApiKeyError, match="NO_SUCH_KEY_REF"):
        await infra.start()
