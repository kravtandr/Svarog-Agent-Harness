"""Приложение chat-TUI (ADR-0018): полноэкранный чат поверх ChatEngine.

Threading-модель: run исполняется async-worker'ом на loop'е Textual, поэтому
хуки прогона — обычные `post_message`. Единственный чужой поток — живой
approval-гейт external-пути (§7): он показывает модалку через
`call_from_thread` и ждёт вердикта на `threading.Event`
(`prompt_gate_from_thread`), решение пишет в БД сам — poll гейта подхватит.

Esc прерывает текущий run: write-ahead trace (ADR-0005) переводит его в
suspended, тёплый sandbox пересобирается перед следующим сообщением.
"""

import asyncio
import contextlib
import threading
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Input
from textual.widgets.markdown import MarkdownStream

from svarog_harness.cli.chat_engine import ChatEngine, ChatEngineProtocol, ChatSessionStart
from svarog_harness.cli.tui.commands import SlashCommand, parse
from svarog_harness.cli.tui.history import InputHistory
from svarog_harness.cli.tui.hooks import (
    PanelEvent,
    ProgressUpdated,
    TextDelta,
    ToolCalled,
    build_tui_hooks,
)
from svarog_harness.cli.tui.screens import (
    ApprovalScreen,
    GateDecision,
    HelpScreen,
    QuestionScreen,
    SessionPickerScreen,
)
from svarog_harness.cli.tui.widgets import (
    ChatInput,
    EventPanel,
    SlashDropdown,
    StatusBar,
    Transcript,
)
from svarog_harness.config.paths import WorkspaceLayoutError
from svarog_harness.config.schema import AutonomyMode, SvarogConfig
from svarog_harness.llm.provider import ChatMessage
from svarog_harness.runtime.loop import RunOutcome
from svarog_harness.runtime.orchestrator import RunHooks
from svarog_harness.storage.models import Approval, RunState

EngineFactory = Callable[[RunHooks], ChatEngineProtocol]


class SvarogChatApp(App[None]):
    """Полноэкранный чат: транскрипт, стрим, гейты, сессии, панель событий."""

    TITLE = "svarog chat"

    CSS: ClassVar[str] = """
    #main-row { height: 1fr; }
    #transcript { padding: 0 1; }
    .user-msg { margin: 1 0 0 0; text-style: bold; color: $accent; }
    .assistant-msg { margin: 0 0 0 2; }
    .note-msg { color: $text-muted; margin: 0 0 0 2; }
    #event-panel { width: 44; border-left: solid $accent; padding: 0 1; }
    #slash-dropdown { border: solid $accent; max-height: 7; }
    #chat-input { border: solid $accent; }
    #status-bar { height: 1; }
    #gate-dialog {
        border: thick $accent; background: $surface; padding: 1 2;
        width: 90; max-width: 90%; max-height: 80%; height: auto;
    }
    #gate-title { text-style: bold; margin-bottom: 1; }
    #gate-body { margin-bottom: 1; }
    #gate-buttons { height: auto; margin-bottom: 1; }
    #gate-buttons Button { margin-right: 2; }
    #picker-dialog {
        border: thick $accent; background: $surface; padding: 1 2;
        width: 120; max-width: 95%; height: 80%;
    }
    #picker-list { width: 1fr; }
    #picker-preview-pane { width: 1fr; border-left: solid $accent; padding: 0 1; }
    ApprovalScreen, QuestionScreen, SessionPickerScreen, HelpScreen { align: center middle; }
    """

    BINDINGS: ClassVar = [
        Binding("escape", "cancel_run", "прервать run", show=False),
        Binding("ctrl+t", "toggle_panel", "события"),
        Binding("ctrl+s", "pick_session", "сессии"),
        Binding("ctrl+n", "new_session", "новая сессия"),
        Binding("ctrl+y", "copy_answer", "копировать ответ"),
        Binding("ctrl+q", "quit", "выход", priority=True),
    ]

    def __init__(
        self,
        cfg: SvarogConfig,
        workspace: Path,
        autonomy: AutonomyMode,
        *,
        continue_ref: str | None = None,
        fork_ref: str | None = None,
        engine_factory: EngineFactory | None = None,
        history_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._cfg = cfg
        self._workspace = workspace
        self._autonomy = autonomy
        self._continue_ref = continue_ref
        self._fork_ref = fork_ref
        self._engine_factory: EngineFactory = engine_factory or (
            lambda hooks: ChatEngine(cfg, workspace, autonomy, hooks)
        )
        self._engine: ChatEngineProtocol | None = None
        self._engine_closed = False
        self._history = InputHistory(history_path)
        self._dropdown = SlashDropdown()
        self._input = ChatInput(self._history, self._dropdown)
        self._transcript = Transcript(id="transcript")
        self._panel = EventPanel()
        self._status = StatusBar(str(workspace), autonomy.value)
        self._stream: MarkdownStream | None = None
        self._run_active = False
        self._quit_armed = False
        self._last_answer = ""  # для Ctrl+Y (терминал не даёт выделять: мышь у TUI)
        self._gate_waiters: set[threading.Event] = set()

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-row"):
            yield self._transcript
            yield self._panel
        yield self._dropdown
        yield self._input
        yield self._status
        yield Footer()

    def on_mount(self) -> None:
        self._input.disabled = True
        self._status.status_text = "поднимаю sandbox…"
        self._status.running = True
        self.run_worker(self._startup(), group="startup", exclusive=True)

    # ------------------------------------------------------------- lifecycle

    async def _startup(self) -> None:
        hooks = build_tui_hooks(self, self._cfg)
        self._engine = self._engine_factory(hooks)
        try:
            start = await self._engine.start(
                continue_ref=self._continue_ref, fork_ref=self._fork_ref
            )
        except Exception as exc:  # SandboxError/ApiKeyError/SessionNotFound/… — показать и встать
            self._status.running = False
            self._status.status_text = "ошибка запуска"
            await self._transcript.add_note(f"[red]ошибка запуска: {exc}[/red]")
            if isinstance(exc, WorkspaceLayoutError):
                await self._transcript.add_note(
                    "[dim]подсказка: chat запускается в каталоге задачи, а не в "
                    "control-plane: svarog chat --workspace "
                    "<agent-home>/workspaces/tasks/<задача>[/dim]"
                )
            await self._transcript.add_note("[dim]Ctrl+Q — выход[/dim]")
            return
        await self._apply_session_start(start)
        self._status.running = False
        self._status.status_text = "готов"
        self._input.disabled = False
        self._input.focus()

    async def _apply_session_start(self, start: ChatSessionStart) -> None:
        if start.label:
            await self._transcript.add_note(f"[dim]{start.label}[/dim]")
        for message in start.history:
            await self._replay_message(message)
        assistants = [m.content for m in start.history if m.role == "assistant"]
        if assistants:
            self._last_answer = assistants[-1]
        self._refresh_session_label()

    async def _replay_message(self, message: ChatMessage) -> None:
        if message.role == "user":
            await self._transcript.add_user(message.content)
        else:
            await self._transcript.add_markdown(message.content)

    def _refresh_session_label(self) -> None:
        engine = self._engine
        session_id = engine.session_id if engine is not None else None
        self._status.session_label = f"сессия {session_id[:8]}" if session_id else "новая сессия"

    async def on_unmount(self) -> None:
        await self._close_engine()

    async def _close_engine(self) -> None:
        if self._engine is None or self._engine_closed:
            return
        self._engine_closed = True
        # Отпустить worker-потоки гейта, если модалки остались без ответа.
        for waiter in list(self._gate_waiters):
            waiter.set()
        # Cleanup — best-effort: терминал уже восстановлен Textual'ом.
        with contextlib.suppress(Exception):
            await self._engine.close()

    # ------------------------------------------------------------- ввод

    @on(Input.Changed, "#chat-input")
    def _on_input_changed(self, event: Input.Changed) -> None:
        self._dropdown.refresh_for(event.value)

    @on(Input.Submitted, "#chat-input")
    async def _on_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        self._dropdown.display = False
        parsed = parse(text)
        if parsed is not None:
            self._input.value = ""
            command, args = parsed
            await self._handle_command(command, args, raw=text)
            return
        if self._run_active:
            await self._transcript.add_note(
                "[yellow]run ещё выполняется — дождитесь ответа или Esc[/yellow]"
            )
            return
        if self._engine is None or self._input.disabled:
            return
        self._history.append(text)
        self._input.value = ""
        self.run_worker(self._send(text), group="run", exclusive=True)

    async def _handle_command(self, command: SlashCommand | None, args: str, *, raw: str) -> None:
        if command is None:
            await self._transcript.add_note(
                f"[yellow]неизвестная команда: {raw} — /help покажет список[/yellow]"
            )
            return
        match command.name:
            case "help":
                self.push_screen(HelpScreen())
            case "quit":
                await self.action_quit()
            case "new":
                self.action_new_session()
            case "sessions":
                self.action_pick_session()
            case "copy":
                self.action_copy_answer()
            case "fork":
                if not args:
                    await self._transcript.add_note("[yellow]нужен id: /fork <ref>[/yellow]")
                    return
                self.run_worker(self._switch_session(args, fork=True), exclusive=True)

    # ------------------------------------------------------------- run

    def _set_running(self, running: bool) -> None:
        self._run_active = running
        self._status.running = running
        self._status.status_text = "агент работает… (Esc — прервать)" if running else "готов"
        if not running:
            self._quit_armed = False
            self._status.progress_text = ""

    async def _finalize_stream(self) -> None:
        if self._stream is not None:
            stream, self._stream = self._stream, None
            # MarkdownStream.stop() отменяет свой фоновый task и await
            # пробрасывает его CancelledError наружу (py3.13+ semantics) —
            # это завершение стрима, а не отмена нашего worker'а.
            with contextlib.suppress(asyncio.CancelledError):
                await stream.stop()

    async def _send(self, task: str) -> None:
        engine = self._engine
        assert engine is not None
        await self._transcript.add_user(task)
        self._set_running(True)
        self._stream = await self._transcript.start_assistant()
        try:
            outcome = await engine.send(task)
        except asyncio.CancelledError:
            await self._finalize_stream()
            await self._transcript.add_note(
                "[yellow]прервано — run будет suspended (svarog resume), "
                "sandbox пересоберётся перед следующим сообщением[/yellow]"
            )
            self._set_running(False)
            return
        except Exception as exc:
            await self._finalize_stream()
            await self._transcript.add_note(f"[red]ошибка run: {exc}[/red]")
            self._set_running(False)
            return
        await self._finalize_stream()
        outcome = await self._native_approval_loop(engine, outcome)
        await self._print_turn(outcome)
        self._last_answer = outcome.final_answer or self._last_answer
        self._refresh_session_label()
        self._set_running(False)

    async def _native_approval_loop(
        self, engine: ChatEngineProtocol, outcome: RunOutcome
    ) -> RunOutcome:
        """WAITING_APPROVAL → модалки решений → resume, пока run не завершится.

        TUI-порт `_interactive_approvals` plain-режима: native-путь создаёт
        approval и суспендится, решение + resume продолжают его на месте.
        """
        while outcome.state is RunState.WAITING_APPROVAL:
            approvals = await engine.pending_approvals(outcome.run_id)
            if not approvals:
                break
            for approval in approvals:
                decision = await self._prompt_gate(approval)
                if approval.action_type == "user.question":
                    await engine.answer_question(
                        approval.id, decision.answer or "", answered_by="chat"
                    )
                else:
                    await engine.decide_approval(
                        approval.id,
                        approved=decision.approved,
                        reason=decision.reason,
                        decided_by="chat",
                    )
            self._stream = await self._transcript.start_assistant()
            try:
                outcome = await engine.resume(outcome.run_id)
            finally:
                await self._finalize_stream()
        return outcome

    async def _print_turn(self, outcome: RunOutcome) -> None:
        """Итоговая строка хода — как `_print_chat_turn` plain-режима."""
        stats = f"{outcome.iterations} итер. | ${outcome.cost_usd:.4f}"
        if outcome.state is RunState.COMPLETED:
            await self._transcript.add_note(f"[dim]— {stats}[/dim]")
        elif outcome.state is RunState.WAITING_APPROVAL:
            await self._transcript.add_note(
                f"[magenta]ожидает approval[/magenta] | {stats} "
                f"[dim](svarog approvals list, затем resume {outcome.run_id[:8]})[/dim]"
            )
        else:
            await self._transcript.add_note(f"[yellow]{outcome.state.value}[/yellow] | {stats}")
            if outcome.error:
                await self._transcript.add_note(f"[yellow]{outcome.error}[/yellow]")

    # ------------------------------------------------------------- гейт

    async def _prompt_gate(self, approval: Approval) -> GateDecision:
        """Модалка гейта на UI-loop'е (native-путь, вызывается из worker'а)."""
        screen: ApprovalScreen | QuestionScreen = (
            QuestionScreen(approval)
            if approval.action_type == "user.question"
            else ApprovalScreen(approval)
        )
        result = await self.push_screen_wait(screen)
        return result or GateDecision(approved=False, reason="закрыто без решения")

    def prompt_gate_from_thread(self, approval: Approval) -> GateDecision:
        """Модалка гейта для worker-потока bridge (§7): блокирует поток, не UI."""
        done = threading.Event()
        box: list[GateDecision] = []
        self._gate_waiters.add(done)

        def show() -> None:
            screen: ApprovalScreen | QuestionScreen = (
                QuestionScreen(approval)
                if approval.action_type == "user.question"
                else ApprovalScreen(approval)
            )

            def finish(result: GateDecision | None) -> None:
                if result is not None:
                    box.append(result)
                done.set()

            self.push_screen(screen, finish)

        self.call_from_thread(show)
        done.wait()
        self._gate_waiters.discard(done)
        return box[0] if box else GateDecision(approved=False, reason="закрыто без решения")

    # ------------------------------------------------------------- события

    @on(TextDelta)
    async def _on_text_delta(self, message: TextDelta) -> None:
        if self._stream is not None:
            await self._stream.write(message.text)

    @on(ToolCalled)
    def _on_tool_called(self, message: ToolCalled) -> None:
        self._log_panel(f"[dim]→ {message.name} {message.args}[/dim]")

    @on(ProgressUpdated)
    def _on_progress(self, message: ProgressUpdated) -> None:
        self._status.progress_text = (
            f"итерация {message.iterations} | {message.tokens} ток. | "
            f"${message.cost:.4f} | контекст {message.context_ratio:.0%}"
        )

    @on(PanelEvent)
    def _on_panel_event(self, message: PanelEvent) -> None:
        colour = {True: "green", False: "yellow", None: "dim"}[message.ok]
        self._log_panel(f"[{colour}]{message.text}[/{colour}]")

    def _log_panel(self, markup: str) -> None:
        self._panel.log_event(markup)
        if not self._panel.display:
            self._status.hidden_events += 1

    @on(SessionPickerScreen.PreviewRequested)
    def _on_preview_requested(self, message: SessionPickerScreen.PreviewRequested) -> None:
        self.run_worker(self._load_preview(message.session_id), group="preview", exclusive=True)

    async def _load_preview(self, session_id: str) -> None:
        engine = self._engine
        if engine is None:
            return
        preview = await engine.session_preview(session_id, limit=6)
        lines = []
        for entry in preview:
            mark = "›" if entry["role"] == "user" else " "
            lines.append(f"{mark} {entry['content'][:200]}")
        screens = [s for s in self.screen_stack if isinstance(s, SessionPickerScreen)]
        if screens:
            screens[-1].show_preview("\n\n".join(lines) or "(пусто)")

    # ------------------------------------------------------------- actions

    def action_cancel_run(self) -> None:
        if self._run_active:
            self.workers.cancel_group(self, "run")

    def action_copy_answer(self) -> None:
        """Скопировать последний ответ агента (OSC 52; мышь захвачена TUI)."""
        if not self._last_answer:
            self.notify("ответов ещё нет — нечего копировать", severity="warning")
            return
        self.copy_to_clipboard(self._last_answer)
        self.notify(f"ответ скопирован ({len(self._last_answer)} символов)")

    def action_toggle_panel(self) -> None:
        self._panel.display = not self._panel.display
        if self._panel.display:
            self._status.hidden_events = 0

    def action_new_session(self) -> None:
        if self._run_active:
            self.notify("run ещё выполняется", severity="warning")
            return
        engine = self._engine
        if engine is None:
            return
        engine.reset_session()
        self._refresh_session_label()
        self.run_worker(self._transcript.add_note("[dim]— новая сессия —[/dim]"), group="note")

    def action_pick_session(self) -> None:
        if self._run_active:
            self.notify("run ещё выполняется", severity="warning")
            return
        self.run_worker(self._pick_session(), group="picker", exclusive=True)

    async def _pick_session(self) -> None:
        engine = self._engine
        if engine is None:
            return
        summaries = await engine.list_sessions(limit=50)
        if not summaries:
            await self._transcript.add_note("[dim]прошлых сессий нет[/dim]")
            return
        result = await self.push_screen_wait(SessionPickerScreen(summaries))
        if result is None:
            return
        ref, fork = result
        await self._switch_session(ref, fork=fork)

    async def _switch_session(self, ref: str, *, fork: bool) -> None:
        engine = self._engine
        if engine is None or self._run_active:
            return
        try:
            start = await engine.switch_session(ref, fork=fork)
        except Exception as exc:  # SessionNotFoundError и пр. — показать, не падать
            await self._transcript.add_note(f"[red]{exc}[/red]")
            return
        await self._transcript.clear_turns()
        await self._apply_session_start(start)

    async def action_quit(self) -> None:
        if self._run_active and not self._quit_armed:
            self._quit_armed = True
            self.notify(
                "run активен — ещё раз Ctrl+Q: прервать (suspended) и выйти",
                severity="warning",
            )
            return
        self.run_worker(self._shutdown(), group="shutdown", exclusive=True)

    async def _shutdown(self) -> None:
        if self._run_active:
            self.workers.cancel_group(self, "run")
        await self._close_engine()
        self.exit()
