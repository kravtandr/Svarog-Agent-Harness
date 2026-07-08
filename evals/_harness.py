"""Общий harness для eval-сценариев (§26, ADR-0008).

Прогоняет задачу через настоящий стек Svarog (AgentLoop, PolicyEngine,
TraceRecorder, sandbox, Flow C, verifier) — без сети: LLM заменён
ScriptedProvider с заранее заданными ходами. Это acceptance-проверка
вертикального среза, а не unit-тест отдельного модуля.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.schema import (
    AutonomyMode,
    GitConfig,
    PoliciesConfig,
    RuntimeConfig,
)
from svarog_harness.gitflow import GitRepo, WorkspaceFlow
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.policy.engine import PolicyEngine
from svarog_harness.runtime.loop import AgentLoop, RunOutcome
from svarog_harness.sandbox.local import LocalEnvironment
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.tools.approval import RequestApprovalTool
from svarog_harness.tools.file_tools import file_tools
from svarog_harness.tools.registry import ToolRegistry
from svarog_harness.tools.shell import BashTool
from svarog_harness.trace.recorder import TraceRecorder


class ScriptedProvider(ModelProvider):
    """LLM-заглушка: отдаёт заранее заданные ходы, запоминает запросы."""

    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)
        self.seen: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.seen.append(list(messages))
        return self.turns.pop(0)


def final(text: str) -> CompletionResult:
    return CompletionResult(content=text, usage=Usage(10, 5), finish_reason="stop")


def call(tool: str, arguments_json: str, *, call_id: str = "c1") -> CompletionResult:
    return CompletionResult(
        content="",
        tool_calls=(ToolCallRequest(id=call_id, name=tool, arguments_json=arguments_json),),
        usage=Usage(10, 5),
        finish_reason="tool_calls",
    )


@dataclass
class EvalHarness:
    workspace: Path
    db_path: Path
    provider: ScriptedProvider
    autonomy: AutonomyMode = AutonomyMode.YOLO
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    policies: PoliciesConfig = field(default_factory=PoliciesConfig)

    def _registry(self, env: LocalEnvironment) -> ToolRegistry:
        registry = ToolRegistry()
        for tool in file_tools(self.workspace):
            registry.register(tool)
        registry.register(BashTool(env))
        registry.register(RequestApprovalTool())
        return registry

    def _loop(self, recorder: TraceRecorder, env: LocalEnvironment) -> AgentLoop:
        return AgentLoop(
            self.provider,
            self._registry(env),
            recorder,
            self.runtime,
            PolicyEngine(autonomy=self.autonomy, policies=self.policies, workspace=self.workspace),
            self.workspace,
            model_name="scripted",
            workspace_flow=WorkspaceFlow(GitRepo(self.workspace), GitConfig()),
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        init_db(self.db_path)
        engine = create_engine(self.db_path)
        factory = create_session_factory(engine)
        try:
            async with factory() as db:
                yield db
        finally:
            await engine.dispose()

    async def run(self, task: str) -> RunOutcome:
        async with self.session() as db:
            env = LocalEnvironment(self.workspace)
            return await self._loop(TraceRecorder(db), env).run(task, self.autonomy)

    async def resume(self, run_id: str) -> RunOutcome:
        async with self.session() as db:
            recorder = TraceRecorder(db)
            run, raw_state = await recorder.load_resumable(run_id)
            from svarog_harness.runtime.checkpoint import LoopState

            env = LocalEnvironment(self.workspace)
            return await self._loop(recorder, env).resume(run, LoopState.from_dict(raw_state))


def make_harness(tmp_path: Path, turns: list[CompletionResult], **kwargs: object) -> EvalHarness:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return EvalHarness(
        workspace=workspace,
        db_path=tmp_path / "state" / "svarog.db",
        provider=ScriptedProvider(turns),
        **kwargs,  # type: ignore[arg-type]
    )
