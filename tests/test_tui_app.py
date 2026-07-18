"""Chat-TUI: pilot-тесты SvarogChatApp с фейковым движком (ChatEngineProtocol)."""

import asyncio
from pathlib import Path

import pytest

from svarog_harness.cli.chat_engine import ChatSessionStart
from svarog_harness.cli.tui.app import SvarogChatApp
from svarog_harness.cli.tui.screens import ApprovalScreen, SessionPickerScreen
from svarog_harness.cli.tui.widgets import SlashDropdown, StatusBar, Transcript
from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import AutonomyMode, SvarogConfig
from svarog_harness.llm.provider import ChatMessage
from svarog_harness.runtime.loop import RunOutcome
from svarog_harness.runtime.orchestrator import RunHooks
from svarog_harness.storage.models import Approval, ApprovalStatus, RunState
from svarog_harness.trace.viewer import SessionSummary


def _outcome(state: RunState = RunState.COMPLETED, run_id: str = "run12345") -> RunOutcome:
    return RunOutcome(
        run_id=run_id,
        state=state,
        final_answer="готово",
        iterations=2,
        tokens_used=100,
        cost_usd=0.0123,
    )


def _approval(action_type: str = "approval.request") -> Approval:
    return Approval(
        id="apr12345",
        run_id="run12345",
        action_type=action_type,
        payload={"tool": "bash", "arguments": {"command": "rm -rf build"}, "reason": "опасно"},
        status=ApprovalStatus.PENDING,
    )


class FakeEngine:
    """Фейк ChatEngineProtocol: скриптованные исходы, запись всех вызовов."""

    def __init__(self, hooks: RunHooks) -> None:
        self.hooks = hooks
        self.started = False
        self.closed = False
        self.sent: list[str] = []
        self.resumed: list[str] = []
        self.decided: list[tuple[str, bool, str | None]] = []
        self.answered: list[tuple[str, str]] = []
        self.rebuilt = 0
        self.outcomes: list[RunOutcome] = []
        self.approvals: list[Approval] = []
        self.stream_text = "привет, **мир**"
        self.send_gate: asyncio.Event | None = None  # None — send мгновенный
        self._session_id: str | None = None
        self.start_result = ChatSessionStart(session_id=None, history=[], label=None)
        self.sessions: list[SessionSummary] = []

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def is_external(self) -> bool:
        return False

    async def start(
        self, *, continue_ref: str | None = None, fork_ref: str | None = None
    ) -> ChatSessionStart:
        self.started = True
        return self.start_result

    async def close(self) -> None:
        self.closed = True

    async def send(self, task: str) -> RunOutcome:
        self.sent.append(task)
        if self.send_gate is not None:
            await self.send_gate.wait()
        if self.hooks.on_text_delta is not None:
            self.hooks.on_text_delta(self.stream_text)
        if self.hooks.on_progress is not None:
            self.hooks.on_progress(2, 100, 0.0123, 0.05)
        self._session_id = "sess5678"
        return self.outcomes.pop(0) if self.outcomes else _outcome()

    async def resume(self, run_id: str) -> RunOutcome:
        self.resumed.append(run_id)
        return self.outcomes.pop(0) if self.outcomes else _outcome()

    async def rebuild_resources(self) -> None:
        self.rebuilt += 1

    async def pending_approvals(self, run_id: str) -> list[Approval]:
        pending, self.approvals = self.approvals, []
        return pending

    async def decide_approval(
        self, approval_id: str, *, approved: bool, reason: str | None, decided_by: str
    ) -> None:
        self.decided.append((approval_id, approved, reason))

    async def answer_question(self, approval_id: str, answer: str, *, answered_by: str) -> None:
        self.answered.append((approval_id, answer))

    async def list_sessions(
        self, *, limit: int = 20, search: str | None = None
    ) -> list[SessionSummary]:
        return self.sessions

    async def session_preview(self, session_id: str, *, limit: int = 6) -> list[dict[str, str]]:
        return [{"role": "user", "content": "старая задача"}]

    async def switch_session(self, ref: str, *, fork: bool) -> ChatSessionStart:
        self._session_id = None if fork else ref
        return ChatSessionStart(
            session_id=self._session_id,
            history=[ChatMessage(role="user", content="старая задача")],
            label=f"{'форк' if fork else 'продолжаю'} сессии {ref[:8]}",
        )

    def reset_session(self) -> None:
        self._session_id = None


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


def _make_app(cfg: SvarogConfig, tmp_path: Path) -> tuple[SvarogChatApp, list[FakeEngine]]:
    engines: list[FakeEngine] = []

    def factory(hooks: RunHooks) -> FakeEngine:
        engine = FakeEngine(hooks)
        engines.append(engine)
        return engine

    app = SvarogChatApp(
        cfg,
        tmp_path,
        AutonomyMode.YOLO,
        engine_factory=factory,
        history_path=tmp_path / "chat_history",
    )
    return app, engines


async def test_submit_streams_and_prints_turn(cfg: SvarogConfig, tmp_path: Path) -> None:
    app, engines = _make_app(cfg, tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        engine = engines[0]
        assert engine.started
        app.query_one("#chat-input").focus()
        await pilot.pause()
        for char in "привет":
            await pilot.press(char)
        await pilot.press("enter")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert engine.sent == ["привет"]
        transcript = app.query_one(Transcript)
        rendered = " ".join(str(w.render()) for w in transcript.query("Static"))
        assert "привет" in rendered
        assert "— 2 итер. | $0.0123" in rendered
        status = app.query_one(StatusBar)
        assert "сессия sess5678" in str(status.render())


async def test_slash_dropdown_and_unknown_command(cfg: SvarogConfig, tmp_path: Path) -> None:
    app, _ = _make_app(cfg, tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        app.query_one("#chat-input").focus()
        await pilot.press("slash")
        await pilot.pause()
        dropdown = app.query_one(SlashDropdown)
        assert dropdown.display and dropdown.option_count == 5
        for char in "нет":
            await pilot.press(char)
        await pilot.press("enter")
        await pilot.pause()
        transcript = app.query_one(Transcript)
        rendered = " ".join(str(w.render()) for w in transcript.query("Static"))
        assert "неизвестная команда" in rendered


async def test_waiting_approval_shows_modal_then_resumes(cfg: SvarogConfig, tmp_path: Path) -> None:
    app, engines = _make_app(cfg, tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        engine = engines[0]
        engine.outcomes = [_outcome(RunState.WAITING_APPROVAL), _outcome(RunState.COMPLETED)]
        engine.approvals = [_approval()]
        app.query_one("#chat-input").focus()
        await pilot.press("g", "o")
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, ApprovalScreen):
                break
        assert isinstance(app.screen, ApprovalScreen)
        await pilot.press("y")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert engine.decided == [("apr12345", True, None)]
        assert engine.resumed == ["run12345"]


async def test_escape_cancels_run(cfg: SvarogConfig, tmp_path: Path) -> None:
    app, engines = _make_app(cfg, tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        engine = engines[0]
        engine.send_gate = asyncio.Event()  # send зависает до отмены
        app.query_one("#chat-input").focus()
        await pilot.press("g", "o")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert engine.sent == ["go"]
        await pilot.press("escape")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        transcript = app.query_one(Transcript)
        rendered = " ".join(str(w.render()) for w in transcript.query("Static"))
        assert "прервано" in rendered


async def test_quit_command_closes_engine(cfg: SvarogConfig, tmp_path: Path) -> None:
    app, engines = _make_app(cfg, tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        app.query_one("#chat-input").focus()
        for char in "/quit":
            await pilot.press(char)
        await pilot.press("enter")
        await pilot.pause()
    assert engines[0].closed


async def test_session_picker_switches_session(cfg: SvarogConfig, tmp_path: Path) -> None:
    from svarog_harness.storage.models import Session

    app, engines = _make_app(cfg, tmp_path)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        engine = engines[0]
        engine.sessions = [
            SessionSummary(
                session=Session(id="sessabcd1234", title="старый чат"),
                runs=3,
                last_task="починить тесты",
            )
        ]
        await pilot.press("ctrl+s")
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, SessionPickerScreen):
                break
        assert isinstance(app.screen, SessionPickerScreen)
        await pilot.press("enter")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        transcript = app.query_one(Transcript)
        rendered = " ".join(str(w.render()) for w in transcript.query("Static"))
        assert "продолжаю сессии sessabcd" in rendered
