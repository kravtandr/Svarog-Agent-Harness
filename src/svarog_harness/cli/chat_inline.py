"""Inline-режим chat (ADR-0018): диалог в обычном буфере терминала.

UX-модель qwen-code/gemini-cli: никакого alt-screen — история диалога уходит
в scrollback терминала (выделяется и копируется нативно), динамична только
нижняя область текущего ответа (Rich Live: хвост стрима + строка прогресса).
По завершении хода Live-область стирается и финальный ответ печатается в
scrollback уже отрендеренным markdown'ом.

Ввод — readline (стрелки, редактирование, персистентная история в
`~/.svarog/chat_history`). Ctrl+C во время run — прервать run (suspended,
ADR-0005); Ctrl+C в промпте — выход. Слэш-команды: /help /new /sessions
/fork /copy /quit.

Работает поверх того же `ChatEngine`, что plain-REPL и полноэкранный TUI;
наблюдение — через `RunHooks`: базовые hooks печатают через console (Rich
Live сам поднимает их вывод над живой областью), переопределяются только
стрим (`on_text_delta` → буфер) и прогресс (`on_progress` → статус-строка).
"""

import asyncio
import base64
import contextlib
import json
import signal
import threading
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from pathlib import Path

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from svarog_harness.cli.chat_commands import COMMANDS, SlashCommand, parse
from svarog_harness.cli.chat_engine import ChatEngine, ChatEngineProtocol
from svarog_harness.config.schema import AutonomyMode, SvarogConfig
from svarog_harness.llm.provider import ChatMessage
from svarog_harness.runtime.loop import RunOutcome
from svarog_harness.runtime.orchestrator import RunHooks
from svarog_harness.storage.models import Approval, RunState
from svarog_harness.trace.viewer import render_sessions_table

_TAIL_LINES = 12  # строк хвоста стрима в живой области; целиком ответ — по завершении

_KEYS_HELP = (
    ("Ctrl+C (во время run)", "прервать run (будет suspended)"),
    ("Ctrl+C / Ctrl+D (в промпте)", "выход"),
    ("↑ / ↓", "история ввода (readline)"),
)


def default_history_path() -> Path:
    """История ввода — в user-state `~/.svarog/` (вне workspace агента)."""
    return Path("~/.svarog/chat_history").expanduser()


def approval_summary(approval: Approval) -> str:
    """Текст карточки approval: действие, tool, аргументы, причина (§12)."""
    payload = approval.payload or {}
    lines = [
        f"[bold]approval {approval.id[:8]}[/bold] | run {approval.run_id[:8]}",
        f"  действие: {approval.action_type}",
    ]
    if payload.get("tool"):
        lines.append(f"  tool: {payload['tool']}")
    if payload.get("arguments"):
        lines.append(
            f"  аргументы: {json.dumps(payload['arguments'], ensure_ascii=False, indent=2)}"
        )
    if payload.get("reason"):
        lines.append(f"  причина: {payload['reason']}")
    return "\n".join(lines)


def _enable_line_editing(history_path: Path) -> None:
    """Readline: стрелки, редактирование, персистентная история.

    С readline `input()` читает строку целиком — старый баг UTF-8 по чанкам
    (см. `_read_user_line` plain-режима) здесь не воспроизводится.
    """
    try:
        import readline
    except ImportError:  # readline недоступен (экзотические сборки) — просто input()
        return
    with contextlib.suppress(Exception):
        if "libedit" in (readline.__doc__ or "").lower():
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
        readline.parse_and_bind("set editing-mode emacs")
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        readline.read_history_file(str(history_path))


def _save_history_line(history_path: Path) -> None:
    try:
        import readline
    except ImportError:
        return
    with contextlib.suppress(OSError):
        readline.set_history_length(1000)
        readline.write_history_file(str(history_path))


def osc52_copy(console: Console, text: str) -> None:
    """Положить текст в буфер обмена терминала (OSC 52; iTerm2/kitty/WezTerm)."""
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    console.file.write(f"\x1b]52;c;{payload}\x07")
    console.file.flush()


class InlineChat:
    """Один диалог inline-режима; тестируется с фейковым движком и вводом."""

    def __init__(
        self,
        cfg: SvarogConfig,
        workspace: Path,
        autonomy: AutonomyMode,
        base_hooks: RunHooks,
        *,
        console: Console | None = None,
        read_line: Callable[[str], Awaitable[str]] | None = None,
        engine_factory: Callable[[RunHooks], ChatEngineProtocol] | None = None,
        history_path: Path | None = None,
    ) -> None:
        self._cfg = cfg
        self._workspace = workspace
        self._autonomy = autonomy
        self._console = console or Console()
        self._read_line = read_line or self._default_read_line
        self._engine_factory = engine_factory or (
            lambda hooks: ChatEngine(cfg, workspace, autonomy, hooks)
        )
        self._history_path = history_path or default_history_path()
        self._hooks = self._build_hooks(base_hooks)
        self._engine: ChatEngineProtocol | None = None
        self._live: Live | None = None
        self._live_lock = threading.Lock()
        self._buffer = ""
        self._progress = ""
        self._last_answer = ""

    # ------------------------------------------------------------- hooks

    def _build_hooks(self, base: RunHooks) -> RunHooks:
        """Стрим и прогресс — в живую область, остальное — печать как в plain.

        Rich Live поднимает обычные `console.print` над живой областью, поэтому
        базовые хуки (tool calls, checks, commit, память) работают без правок.
        Живой approval-промпт оборачивается паузой Live: prompts и перерисовка
        не должны писать в терминал одновременно.
        """
        original_gate = base.on_approval_requested

        def gate(approval: Approval) -> None:
            if original_gate is None:
                return
            with self._live_paused():
                original_gate(approval)

        return replace(
            base,
            on_text_delta=self._on_delta,
            on_progress=self._on_progress,
            on_approval_requested=gate if original_gate is not None else None,
        )

    def _on_delta(self, delta: str) -> None:
        with self._live_lock:
            self._buffer += delta
            if self._live is not None:
                self._live.update(self._render_live())

    def _on_progress(self, iterations: int, tokens: int, cost: float, ratio: float) -> None:
        with self._live_lock:
            self._progress = (
                f"итерация {iterations} | {tokens} ток. | ${cost:.4f} | контекст {ratio:.0%}"
            )
            if self._live is not None:
                self._live.update(self._render_live())

    def _render_live(self) -> Group:
        tail = "\n".join(self._buffer.splitlines()[-_TAIL_LINES:])
        status = self._progress or "агент работает… (Ctrl+C — прервать)"
        return Group(Text(tail), Text.from_markup(f"[dim]⠿ {status}[/dim]"))

    @contextlib.contextmanager
    def _live_paused(self) -> Iterator[None]:
        """Убрать живую область на время блокирующего промпта и вернуть после."""
        with self._live_lock:
            live = self._live
            if live is not None:
                live.stop()
        try:
            yield
        finally:
            with self._live_lock:
                if live is not None and self._live is live:
                    live.start()

    # ------------------------------------------------------------- ввод

    async def _default_read_line(self, prompt: str) -> str:
        return await asyncio.to_thread(input, prompt)

    # ------------------------------------------------------------- диалог

    async def run(self, *, continue_ref: str | None = None, fork_ref: str | None = None) -> None:
        console = self._console
        _enable_line_editing(self._history_path)
        engine = self._engine_factory(self._hooks)
        self._engine = engine
        console.print(
            f"[bold]svarog chat[/bold] | workspace: {self._workspace} | "
            f"автономия: {self._autonomy.value}\n"
            f"[dim]/help — команды; Ctrl+C в промпте или /quit — выход[/dim]"
        )
        with console.status("[dim]поднимаю sandbox…[/dim]"):
            start = await engine.start(continue_ref=continue_ref, fork_ref=fork_ref)
        try:
            if start.label:
                console.print(f"[dim]{start.label}[/dim]")
            for message in start.history:
                self._print_history_message(message)
            while True:
                try:
                    line = (await self._read_line("\n› ")).strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not line:
                    continue
                parsed = parse(line)
                if parsed is not None:
                    command, args = parsed
                    if await self._handle_command(command, args, raw=line):
                        break
                    continue
                _save_history_line(self._history_path)
                await self._run_task(line)
        finally:
            await engine.close()

    def _print_history_message(self, message: ChatMessage) -> None:
        if message.role == "user":
            self._console.print(Text(f"› {message.content}", style="bold cyan"))
        else:
            self._console.print(Markdown(message.content))
            self._last_answer = message.content

    # ------------------------------------------------------------- run

    async def _run_task(self, task: str) -> None:
        engine = self._engine
        assert engine is not None
        console = self._console
        with self._live_lock:
            self._buffer = ""
            self._progress = ""
            live = Live(
                self._render_live(),
                console=console,
                refresh_per_second=8,
                transient=True,  # по завершении область стирается, ответ печатается заново
            )
            self._live = live
        live.start()
        send = asyncio.ensure_future(engine.send(task))
        self._install_sigint(send)
        try:
            outcome = await send
        except asyncio.CancelledError:
            self._teardown_live()
            console.print(
                "[yellow]прервано — run будет suspended (svarog resume), "
                "sandbox пересоберётся перед следующим сообщением[/yellow]"
            )
            return
        except Exception as exc:
            self._teardown_live()
            console.print(f"[red]ошибка run: {exc}[/red]")
            return
        finally:
            self._remove_sigint()
        self._teardown_live()
        self._print_answer(outcome)
        outcome = await self._native_approval_loop(engine, outcome)
        self._print_turn_footer(outcome)

    def _teardown_live(self) -> None:
        with self._live_lock:
            if self._live is not None:
                self._live.stop()
                self._live = None

    def _print_answer(self, outcome: RunOutcome) -> None:
        answer = outcome.final_answer or "(без ответа)"
        self._console.print(Markdown(answer))
        self._last_answer = outcome.final_answer or self._last_answer

    def _print_turn_footer(self, outcome: RunOutcome) -> None:
        console = self._console
        stats = f"{outcome.iterations} итер. | ${outcome.cost_usd:.4f}"
        if outcome.state is RunState.COMPLETED:
            console.print(f"[dim]— {stats}[/dim]")
        elif outcome.state is RunState.WAITING_APPROVAL:
            console.print(
                f"[magenta]ожидает approval[/magenta] | {stats} "
                f"[dim](svarog approvals list, затем resume {outcome.run_id[:8]})[/dim]"
            )
        else:
            console.print(f"[yellow]{outcome.state.value}[/yellow] | {stats}")
            if outcome.error:
                console.print(f"[yellow]{outcome.error}[/yellow]")

    # Ctrl+C во время run: первый — прервать run, второй — обычный KeyboardInterrupt.
    def _install_sigint(self, task: "asyncio.Future[RunOutcome]") -> None:
        loop = asyncio.get_running_loop()

        def cancel_run() -> None:
            if not task.done():
                task.cancel()

        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal.SIGINT, cancel_run)

    def _remove_sigint(self) -> None:
        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.remove_signal_handler(signal.SIGINT)

    # ------------------------------------------------------------- native approvals

    async def _native_approval_loop(
        self, engine: ChatEngineProtocol, outcome: RunOutcome
    ) -> RunOutcome:
        """WAITING_APPROVAL → промпт решения → resume, пока run не завершится."""
        console = self._console
        while outcome.state is RunState.WAITING_APPROVAL:
            approvals = await engine.pending_approvals(outcome.run_id)
            if not approvals:
                break
            for approval in approvals:
                console.print()
                if approval.action_type == "user.question":
                    payload = approval.payload or {}
                    console.print(
                        f"[bold]вопрос {approval.id[:8]}[/bold] | run {approval.run_id[:8]}\n"
                        f"  [cyan]{payload.get('question') or payload.get('reason') or ''}[/cyan]"
                    )
                    answer = await asyncio.to_thread(
                        typer.prompt,
                        "ваш ответ (Enter — продолжить без ответа)",
                        default="",
                        show_default=False,
                    )
                    await engine.answer_question(approval.id, answer, answered_by="chat")
                    continue
                console.print(approval_summary(approval))
                approved = await asyncio.to_thread(typer.confirm, "одобрить действие?", False)
                reason = None
                if not approved:
                    reason = (
                        await asyncio.to_thread(
                            typer.prompt, "причина отказа", default="", show_default=False
                        )
                        or None
                    )
                await engine.decide_approval(
                    approval.id, approved=approved, reason=reason, decided_by="chat"
                )
            with self._live_lock:
                self._buffer = ""
                self._progress = ""
                live = Live(
                    self._render_live(), console=console, refresh_per_second=8, transient=True
                )
                self._live = live
            live.start()
            try:
                outcome = await engine.resume(outcome.run_id)
            finally:
                self._teardown_live()
            self._print_answer(outcome)
        return outcome

    # ------------------------------------------------------------- команды

    async def _handle_command(self, command: SlashCommand | None, args: str, *, raw: str) -> bool:
        """True — выйти из чата."""
        console = self._console
        engine = self._engine
        assert engine is not None
        if command is None:
            console.print(f"[yellow]неизвестная команда: {raw} — /help покажет список[/yellow]")
            return False
        match command.name:
            case "quit":
                return True
            case "help":
                self._print_help()
            case "new":
                engine.reset_session()
                console.print("[dim]— новая сессия —[/dim]")
            case "copy":
                if not self._last_answer:
                    console.print("[yellow]ответов ещё нет — нечего копировать[/yellow]")
                else:
                    osc52_copy(console, self._last_answer)
                    console.print(
                        f"[dim]ответ скопирован ({len(self._last_answer)} символов)[/dim]"
                    )
            case "fork":
                if not args:
                    console.print("[yellow]нужен id: /fork <ref>[/yellow]")
                else:
                    await self._switch(args, fork=True)
            case "sessions":
                await self._pick_session()
        return False

    def _print_help(self) -> None:
        lines = ["[bold]слэш-команды[/bold]"]
        lines += [f"  {cmd.usage:<18} {cmd.help}" for cmd in COMMANDS]
        lines.append("")
        lines.append("[bold]клавиши[/bold]")
        lines += [f"  {key:<28} {desc}" for key, desc in _KEYS_HELP]
        lines.append("")
        lines.append("[dim]текст выделяется и копируется как обычно — alt-screen нет[/dim]")
        self._console.print("\n".join(lines))

    async def _switch(self, ref: str, *, fork: bool) -> None:
        engine = self._engine
        assert engine is not None
        try:
            start = await engine.switch_session(ref, fork=fork)
        except Exception as exc:  # SessionNotFoundError и пр.
            self._console.print(f"[red]{exc}[/red]")
            return
        if start.label:
            self._console.print(f"[dim]{start.label}[/dim]")
        for message in start.history:
            self._print_history_message(message)

    async def _pick_session(self) -> None:
        engine = self._engine
        assert engine is not None
        console = self._console
        summaries = await engine.list_sessions(limit=20)
        if not summaries:
            console.print("[dim]прошлых сессий нет[/dim]")
            return
        console.print(render_sessions_table(summaries))
        choice = (
            await self._read_line("id для продолжения (f <id> — форк, Enter — отмена): ")
        ).strip()
        if not choice:
            return
        fork = choice.startswith("f ")
        await self._switch(choice.removeprefix("f ").strip(), fork=fork)


async def run_chat_inline(
    cfg: SvarogConfig,
    workspace: Path,
    autonomy: AutonomyMode,
    base_hooks: RunHooks,
    *,
    continue_ref: str | None = None,
    fork_ref: str | None = None,
) -> None:
    """Точка входа inline-режима из `svarog chat` (loop создаёт вызывающий)."""
    chat = InlineChat(cfg, workspace, autonomy, base_hooks)
    await chat.run(continue_ref=continue_ref, fork_ref=fork_ref)
