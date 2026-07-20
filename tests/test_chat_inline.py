"""Inline-режим chat (ADR-0018): диалог с фейковым движком и скриптованным вводом."""

import io
from pathlib import Path

import pytest
from rich.console import Console

from svarog_harness.cli.chat_engine import ChatSessionStart
from svarog_harness.cli.chat_inline import InlineChat
from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import AutonomyMode, SvarogConfig
from svarog_harness.llm.provider import ChatMessage
from svarog_harness.runtime.loop import RunOutcome
from svarog_harness.runtime.orchestrator import RunHooks
from svarog_harness.storage.models import Approval, ApprovalStatus, RunState


def _outcome(state: RunState = RunState.COMPLETED, run_id: str = "run12345") -> RunOutcome:
    return RunOutcome(
        run_id=run_id,
        state=state,
        final_answer="**готово**",
        iterations=2,
        tokens_used=100,
        cost_usd=0.0123,
    )


class FakeEngine:
    """Фейк ChatEngineProtocol: скриптованные исходы, запись всех вызовов."""

    def __init__(self, hooks: RunHooks) -> None:
        self.hooks = hooks
        self.closed = False
        self.sent: list[str] = []
        self.resumed: list[str] = []
        self.decided: list[tuple[str, bool, str | None]] = []
        self.outcomes: list[RunOutcome] = []
        self.approvals: list[Approval] = []
        self.reset_calls = 0
        self._session_id: str | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def is_external(self) -> bool:
        return False

    async def start(
        self, *, continue_ref: str | None = None, fork_ref: str | None = None
    ) -> ChatSessionStart:
        return ChatSessionStart(session_id=None, history=[], label=None)

    async def close(self) -> None:
        self.closed = True

    async def send(self, task: str) -> RunOutcome:
        self.sent.append(task)
        if self.hooks.on_text_delta is not None:
            self.hooks.on_text_delta("**гото")
            self.hooks.on_text_delta("во**")
        if self.hooks.on_progress is not None:
            self.hooks.on_progress(2, 100, 0.0123, 0.05)
        self._session_id = "sess5678"
        return self.outcomes.pop(0) if self.outcomes else _outcome()

    async def resume(self, run_id: str) -> RunOutcome:
        self.resumed.append(run_id)
        return self.outcomes.pop(0) if self.outcomes else _outcome()

    async def rebuild_resources(self) -> None:
        pass

    async def pending_approvals(self, run_id: str) -> list[Approval]:
        pending, self.approvals = self.approvals, []
        return pending

    async def decide_approval(
        self, approval_id: str, *, approved: bool, reason: str | None, decided_by: str
    ) -> None:
        self.decided.append((approval_id, approved, reason))

    async def answer_question(self, approval_id: str, answer: str, *, answered_by: str) -> None:
        pass

    async def list_sessions(self, *, limit: int = 20, search: str | None = None) -> list:
        return []

    async def session_preview(self, session_id: str, *, limit: int = 6) -> list[dict[str, str]]:
        return []

    async def switch_session(self, ref: str, *, fork: bool) -> ChatSessionStart:
        return ChatSessionStart(
            session_id=None if fork else ref,
            history=[ChatMessage(role="assistant", content="из истории")],
            label="продолжаю сессию " + ref[:8],
        )

    def reset_session(self) -> None:
        self.reset_calls += 1


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SvarogConfig:
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    return load_config(project_dir=ws)


def _chat(
    cfg: SvarogConfig, tmp_path: Path, inputs: list[str]
) -> tuple[InlineChat, list[FakeEngine], Console]:
    console = Console(file=io.StringIO(), width=100, force_terminal=False)
    engines: list[FakeEngine] = []
    script = list(inputs)

    async def read_line(prompt: str) -> str:
        if not script:
            raise EOFError
        return script.pop(0)

    def factory(hooks: RunHooks) -> FakeEngine:
        engine = FakeEngine(hooks)
        engines.append(engine)
        return engine

    chat = InlineChat(
        cfg,
        tmp_path,
        AutonomyMode.YOLO,
        RunHooks(),
        console=console,
        read_line=read_line,
        engine_factory=factory,
        history_path=tmp_path / "chat_history",
    )
    return chat, engines, console


def _output(console: Console) -> str:
    file = console.file
    assert isinstance(file, io.StringIO)
    return file.getvalue()


async def test_send_prints_markdown_answer_and_footer(cfg: SvarogConfig, tmp_path: Path) -> None:
    chat, engines, console = _chat(cfg, tmp_path, ["привет"])
    await chat.run()
    engine = engines[0]
    assert engine.sent == ["привет"] and engine.closed
    out = _output(console)
    assert "готово" in out  # markdown-ответ напечатан в scrollback
    assert "— 2 итер. · $0.0123" in out


async def test_quit_and_unknown_command(cfg: SvarogConfig, tmp_path: Path) -> None:
    chat, engines, console = _chat(cfg, tmp_path, ["/нет", "/quit", "не дойдёт"])
    await chat.run()
    assert engines[0].sent == []
    out = _output(console)
    assert "неизвестная команда" in out


async def test_new_and_copy_commands(cfg: SvarogConfig, tmp_path: Path) -> None:
    chat, engines, console = _chat(cfg, tmp_path, ["/copy", "go", "/new", "/copy"])
    await chat.run()
    engine = engines[0]
    assert engine.reset_calls == 1
    out = _output(console)
    assert "нечего копировать" in out  # /copy до первого ответа
    assert "\x1b]52;c;" in out  # OSC 52 после ответа
    assert "ответ скопирован" in out


async def test_waiting_approval_prompts_and_resumes(
    cfg: SvarogConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat, engines, console = _chat(cfg, tmp_path, ["go"])

    def factory_patch() -> None:
        engine = engines[0]
        engine.outcomes = [_outcome(RunState.WAITING_APPROVAL), _outcome(RunState.COMPLETED)]
        engine.approvals = [
            Approval(
                id="apr12345",
                run_id="run12345",
                action_type="approval.request",
                payload={"tool": "bash", "arguments": {"command": "rm -rf build"}},
                status=ApprovalStatus.PENDING,
            )
        ]

    import typer

    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)

    orig_run_task = chat._run_task

    async def run_task(task: str) -> None:
        factory_patch()
        await orig_run_task(task)

    monkeypatch.setattr(chat, "_run_task", run_task)
    await chat.run()
    engine = engines[0]
    assert engine.decided == [("apr12345", True, None)]
    assert engine.resumed == ["run12345"]
    out = _output(console)
    assert "approval apr12345" in out


async def test_switch_session_prints_history(cfg: SvarogConfig, tmp_path: Path) -> None:
    chat, _engines, console = _chat(cfg, tmp_path, ["/fork abc12345", "/quit"])
    await chat.run()
    out = _output(console)
    assert "продолжаю сессию abc12345" in out
    assert "из истории" in out
