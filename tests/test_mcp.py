"""Тесты MCP-интеграции (#29): discovery, MCPTool, default require_approval (§9)."""

from collections.abc import Callable
from pathlib import Path

import pytest

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import AutonomyMode, PoliciesConfig, PolicyProfile
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.mcp import MCPBackend, MCPError, MCPTool, MCPToolSpec, build_mcp_tools
from svarog_harness.policy.engine import PolicyAction, PolicyEngine
from svarog_harness.runtime import orchestrator
from svarog_harness.runtime.orchestrator import RunHooks, TaskRunner
from svarog_harness.tools.base import RiskLevel


class FakeBackend(MCPBackend):
    def __init__(
        self, server_name: str, specs: list[MCPToolSpec], risk: RiskLevel = RiskLevel.HIGH
    ) -> None:
        self.server_name = server_name
        self.risk = risk
        self._specs = specs
        self.calls: list[tuple[str, dict[str, object]]] = []

    def specs(self) -> list[MCPToolSpec]:
        return self._specs

    async def call(self, tool: str, arguments: dict[str, object]) -> str:
        self.calls.append((tool, arguments))
        if tool == "boom":
            raise MCPError("сервер упал")
        return f"echo:{arguments.get('text', '')}"


_SPEC = MCPToolSpec(
    name="echo",
    description="Повторяет текст.",
    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
)


def _policy(profiles: dict[str, PolicyProfile] | None = None) -> PolicyEngine:
    return PolicyEngine(
        autonomy=AutonomyMode.YOLO,
        policies=PoliciesConfig(profiles=profiles or {}),
        workspace=Path.cwd(),
    )


def test_build_mcp_tools_names_and_action_types() -> None:
    backend = FakeBackend("weather", [_SPEC, MCPToolSpec(name="forecast", description="")])
    tools = build_mcp_tools([backend])
    assert [t.name for t in tools] == ["mcp__weather__echo", "mcp__weather__forecast"]
    assert tools[0].action_type == "mcp.weather.echo"
    assert tools[0].risk_level is RiskLevel.HIGH


def test_mcp_tool_definition_uses_input_schema() -> None:
    tool = MCPTool(FakeBackend("weather", [_SPEC]), _SPEC)
    definition = tool.definition()
    assert definition.input_schema["properties"] == {"text": {"type": "string"}}


async def test_mcp_tool_call_success_and_error() -> None:
    backend = FakeBackend("weather", [_SPEC])
    echo = MCPTool(backend, _SPEC)
    result = await echo.call({"text": "привет"})
    assert result.ok
    assert result.output == "echo:привет"

    boom_spec = MCPToolSpec(name="boom", description="падает")
    boom = MCPTool(backend, boom_spec)
    failed = await boom.call({})
    assert not failed.ok
    assert "сервер упал" in (failed.error or "")


def test_mcp_tool_requires_approval_by_default() -> None:
    tool = MCPTool(FakeBackend("weather", [_SPEC]), _SPEC)
    decision = _policy().evaluate(tool, {"text": "x"})
    # По умолчанию MCP tool требует approval в любом режиме (§9), даже в yolo.
    assert decision.action is PolicyAction.REQUIRE_APPROVAL
    assert decision.action_type == "mcp.weather.echo"


def test_mcp_tool_relaxed_by_notify_profile() -> None:
    profiles = {"yolo": PolicyProfile(notify=["mcp.weather.*"])}
    tool = MCPTool(FakeBackend("weather", [_SPEC]), _SPEC)
    decision = _policy(profiles).evaluate(tool, {"text": "x"})
    assert decision.action is PolicyAction.NOTIFY


# --- интеграция: MCP tool доступен агенту и упирается в approval ---


def _write_config(ws: Path, tmp_path: Path) -> None:
    (ws / "svarog.yaml").write_text(
        "models:\n  default: local\n  providers:\n    local:\n"
        "      base_url: http://localhost:9/v1\n      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )


def _patch_provider(monkeypatch: pytest.MonkeyPatch, turns: list[CompletionResult]) -> None:
    class ScriptedProvider(ModelProvider):
        def __init__(self) -> None:
            self.turns = list(turns)

        async def complete(
            self,
            messages: list[ChatMessage],
            tools: list[ToolDefinition],
            *,
            on_text_delta: Callable[[str], None] | None = None,
        ) -> CompletionResult:
            return self.turns.pop(0)

    provider = ScriptedProvider()
    monkeypatch.setattr(orchestrator, "default_provider", lambda models_cfg, store=None: provider)


async def test_run_mcp_tool_gated_by_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    backend = FakeBackend("weather", [_SPEC])

    async def fake_connect(cfg: object, store: object = None) -> list[MCPBackend]:
        return [backend]

    monkeypatch.setattr(orchestrator, "connect_mcp_servers", fake_connect)

    _patch_provider(
        monkeypatch,
        [
            CompletionResult(
                content="",
                tool_calls=(
                    ToolCallRequest(
                        id="c1",
                        name="mcp__weather__echo",
                        arguments_json='{"text": "привет"}',
                    ),
                ),
                usage=Usage(10, 5),
            ),
        ],
    )
    runner = TaskRunner(load_config(project_dir=ws), ws)
    outcome = await runner.run_once("вызови mcp", AutonomyMode.YOLO, hooks=RunHooks())

    # MCP tool по умолчанию требует approval → run уходит в waiting_approval,
    # инструмент ещё не исполнялся.
    assert outcome.state.value == "waiting_approval"
    assert backend.calls == []


# --- ADR-0015 фаза 2: deferred-схемы MCP-tools за флагом mcp.defer_schemas ---


def test_defer_schemas_default_auto() -> None:
    from svarog_harness.config.schema import MCPConfig

    # "auto": deferred включается сам при 10+ MCP-tools; явные true/false — приоритет.
    assert MCPConfig().defer_schemas == "auto"


class _DefsCapturingProvider(ModelProvider):
    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)
        self.seen_defs: list[list[ToolDefinition]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.seen_defs.append(list(tools))
        return self.turns.pop(0)


def _wire_deferred_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    defer: bool | str = "auto",
    tool_count: int = 1,
) -> tuple[TaskRunner, _DefsCapturingProvider]:
    ws = tmp_path / "ws"
    ws.mkdir()
    mcp_section = "" if defer == "auto" else f"mcp:\n  defer_schemas: {str(defer).lower()}\n"
    (ws / "svarog.yaml").write_text(
        "models:\n  default: local\n  providers:\n    local:\n"
        "      base_url: http://localhost:9/v1\n      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"{mcp_section}"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    specs = [_SPEC] + [
        MCPToolSpec(name=f"extra{i}", description=f"Инструмент {i}.") for i in range(tool_count - 1)
    ]
    backend = FakeBackend("weather", specs)

    async def fake_connect(cfg: object, store: object = None) -> list[MCPBackend]:
        return [backend]

    monkeypatch.setattr(orchestrator, "connect_mcp_servers", fake_connect)
    provider = _DefsCapturingProvider(
        [CompletionResult(content="готово", usage=Usage(10, 5), finish_reason="stop")]
    )
    monkeypatch.setattr(orchestrator, "default_provider", lambda models_cfg, store=None: provider)
    return TaskRunner(load_config(project_dir=ws), ws), provider


async def test_mcp_schemas_deferred_behind_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, provider = _wire_deferred_run(tmp_path, monkeypatch, defer=True)
    outcome = await runner.run_once("задача", AutonomyMode.YOLO, hooks=RunHooks())
    assert outcome.state.value == "completed"

    names = [d.name for d in provider.seen_defs[0]]
    # Полная схема MCP-tool скрыта; вместо неё — load_tool со сводкой.
    assert "mcp__weather__echo" not in names
    assert "load_tool" in names
    load_def = next(d for d in provider.seen_defs[0] if d.name == "load_tool")
    assert "mcp__weather__echo — Повторяет текст." in load_def.description


async def test_mcp_schemas_full_when_flag_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, provider = _wire_deferred_run(tmp_path, monkeypatch, defer=False)
    outcome = await runner.run_once("задача", AutonomyMode.YOLO, hooks=RunHooks())
    assert outcome.state.value == "completed"

    names = [d.name for d in provider.seen_defs[0]]
    assert "mcp__weather__echo" in names
    assert "load_tool" not in names


async def test_mcp_auto_defers_at_ten_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Автогейт (ADR-0015 фаза 2): 10+ MCP-tools при defer_schemas=auto → deferred."""
    runner, provider = _wire_deferred_run(tmp_path, monkeypatch, tool_count=10)
    outcome = await runner.run_once("задача", AutonomyMode.YOLO, hooks=RunHooks())
    assert outcome.state.value == "completed"

    names = [d.name for d in provider.seen_defs[0]]
    assert "load_tool" in names
    assert not any(name.startswith("mcp__") for name in names)


async def test_mcp_auto_keeps_full_below_ten_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, provider = _wire_deferred_run(tmp_path, monkeypatch, tool_count=9)
    outcome = await runner.run_once("задача", AutonomyMode.YOLO, hooks=RunHooks())
    assert outcome.state.value == "completed"

    names = [d.name for d in provider.seen_defs[0]]
    assert "load_tool" not in names
    assert sum(1 for name in names if name.startswith("mcp__")) == 9


async def test_mcp_explicit_false_overrides_auto_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, provider = _wire_deferred_run(tmp_path, monkeypatch, defer=False, tool_count=12)
    outcome = await runner.run_once("задача", AutonomyMode.YOLO, hooks=RunHooks())
    assert outcome.state.value == "completed"

    names = [d.name for d in provider.seen_defs[0]]
    assert "load_tool" not in names
    assert sum(1 for name in names if name.startswith("mcp__")) == 12
