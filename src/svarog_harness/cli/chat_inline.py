"""Inline-режим chat (ADR-0018): диалог в обычном буфере терминала.

Макет как у Claude Code / qwen-code: welcome-бокс с логотипом, полоса над
промптом, статус внизу. Подсказки `/` и `@` — только при наборе (паттерн
CompletionMode из qwen-code), через prompt_toolkit. Без alt-screen: история
в scrollback, живая область — только стрим текущего ответа (Rich Live).
"""

import asyncio
import base64
import contextlib
import json
import signal
import threading
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import typer
from prompt_toolkit import PromptSession
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from svarog_harness.cli.chat_commands import COMMANDS, SlashCommand, parse
from svarog_harness.cli.chat_display import (
    ACCENT,
    ChatStatusView,
    chat_status_view,
    format_tool_call,
    format_user_message,
    input_separator,
    session_model_label,
    turn_rule,
    welcome_panel,
)
from svarog_harness.cli.chat_engine import ChatEngine, ChatEngineProtocol
from svarog_harness.cli.chat_picker import pick_option
from svarog_harness.cli.chat_prompt import make_prompt_session, prompt_chat_line
from svarog_harness.cli.chat_settings import (
    SettingsApplyError,
    apply_executor_label,
    apply_mode,
    apply_policies,
    executor_yaml_patch,
    patch_project_config,
    policies_yaml_patch,
)
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
    ("↑ / ↓", "история ввода / меню выбора"),
    ("/ …", "меню слэш-команд при наборе"),
    ("@ …", "подсказки файлов workspace"),
)

PickOptionFn = Callable[[str, Sequence[tuple[str, str]], str | None], Awaitable[str | None]]


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
        allow_layout_overlap: bool = False,
        pick_option_fn: PickOptionFn | None = None,
    ) -> None:
        self._cfg = cfg
        self._workspace = workspace
        self._autonomy = autonomy
        self._console = console or Console()
        self._use_prompt_toolkit = read_line is None
        self._read_line = read_line or self._default_read_line
        self._engine_factory = engine_factory or (
            lambda hooks: ChatEngine(
                cfg, workspace, autonomy, hooks, allow_layout_overlap=allow_layout_overlap
            )
        )
        self._history_path = history_path or default_history_path()
        self._hooks = self._build_hooks(base_hooks)
        self._engine: ChatEngineProtocol | None = None
        self._prompt_session: PromptSession[str] | None = None
        self._live: Live | None = None
        self._live_lock = threading.Lock()
        self._buffer = ""
        self._progress = ""
        self._last_answer = ""
        self._pick_option = pick_option_fn or (
            lambda title, values, default: pick_option(title, values, default=default)
        )

    # ------------------------------------------------------------- hooks

    def _build_hooks(self, base: RunHooks) -> RunHooks:
        """Стрим и прогресс — в живую область; tool-вызовы — краткие карточки."""
        original_gate = base.on_approval_requested

        def gate(approval: Approval) -> None:
            if original_gate is None:
                return
            with self._live_paused():
                original_gate(approval)

        def tool_call(name: str, args: dict[str, object]) -> None:
            self._console.print(format_tool_call(name, args))

        return replace(
            base,
            on_text_delta=self._on_delta,
            on_progress=self._on_progress,
            on_tool_call=tool_call,
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
        spinner = Text()
        spinner.append("⠿ ", style=ACCENT)
        spinner.append(status, style="dim")
        return Group(Text(tail) if tail else Text(""), spinner)

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
        if prompt == "› " and self._prompt_session is not None:
            return await asyncio.to_thread(prompt_chat_line, self._prompt_session)
        return await asyncio.to_thread(input, prompt)

    # ------------------------------------------------------------- диалог

    async def run(self, *, continue_ref: str | None = None, fork_ref: str | None = None) -> None:
        console = self._console
        engine = self._engine_factory(self._hooks)
        self._engine = engine
        status = chat_status_view(self._cfg, self._autonomy)
        model = session_model_label(self._cfg)
        if self._use_prompt_toolkit:
            self._prompt_session = make_prompt_session(self._workspace, self._history_path, status)
        console.print(welcome_panel(self._workspace, status, model=model))
        with console.status("[dim]поднимаю sandbox…[/dim]", spinner="dots"):
            start = await engine.start(continue_ref=continue_ref, fork_ref=fork_ref)
        try:
            if start.label:
                console.print(f"[dim]{start.label}[/dim]")
            for message in start.history:
                self._print_history_message(message)
            while True:
                console.print()
                # Полосы вокруг › рисует prompt_toolkit (сэндвич как у Claude Code).
                # В тестах/plain-подмене read_line — Rich Rule сверху и снизу.
                if not self._use_prompt_toolkit:
                    console.print(input_separator())
                try:
                    line = (await self._read_line("› ")).strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not self._use_prompt_toolkit:
                    console.print(input_separator())
                if not line:
                    continue
                parsed = parse(line)
                if parsed is not None:
                    command, args = parsed
                    if await self._handle_command(command, args, raw=line):
                        break
                    continue
                await self._run_task(line)
        finally:
            await engine.close()

    def _print_history_message(self, message: ChatMessage) -> None:
        if message.role == "user":
            self._console.print(format_user_message(message.content))
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
                transient=True,
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
        self._console.print()
        self._console.print(Markdown(answer))
        self._last_answer = outcome.final_answer or self._last_answer

    def _print_turn_footer(self, outcome: RunOutcome) -> None:
        console = self._console
        stats = f"{outcome.iterations} итер. · ${outcome.cost_usd:.4f}"
        console.print(turn_rule())
        if outcome.state is RunState.COMPLETED:
            console.print(f"[dim]— {stats}[/dim]")
        elif outcome.state is RunState.WAITING_APPROVAL:
            console.print(
                f"[magenta]ожидает approval[/magenta] · {stats} "
                f"[dim](svarog approvals list, затем resume {outcome.run_id[:8]})[/dim]"
            )
        else:
            console.print(f"[yellow]{outcome.state.value}[/yellow] · {stats}")
            if outcome.error:
                console.print(f"[yellow]{outcome.error}[/yellow]")

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
            case "executor":
                await self._cmd_executor()
            case "mode":
                await self._cmd_mode()
            case "policies":
                await self._cmd_policies()
        return False

    def _status_view(self) -> ChatStatusView:
        return chat_status_view(self._cfg, self._autonomy)

    def _refresh_prompt_status(self) -> None:
        if self._use_prompt_toolkit:
            self._prompt_session = make_prompt_session(
                self._workspace, self._history_path, self._status_view()
            )

    async def _apply_cfg(
        self,
        cfg: SvarogConfig,
        autonomy: AutonomyMode,
        *,
        yaml_patch: dict[str, object],
        label: str,
    ) -> None:
        engine = self._engine
        assert engine is not None
        try:
            path = patch_project_config(self._workspace, yaml_patch)
        except SettingsApplyError as exc:
            self._console.print(f"[red]{exc}[/red]")
            return
        self._cfg = cfg
        self._autonomy = autonomy
        await engine.reconfigure(cfg, autonomy)
        self._refresh_prompt_status()
        self._console.print(f"[dim]{label} · сохранено в {path.name}[/dim]")

    async def _cmd_executor(self) -> None:
        status = self._status_view()
        values = [(label, label) for label in status.executors]
        choice = await self._pick_option("executor", values, status.active_executor)
        if choice is None or choice == status.active_executor:
            return
        try:
            cfg = apply_executor_label(self._cfg, choice)
        except SettingsApplyError as exc:
            self._console.print(f"[yellow]{exc}[/yellow]")
            return
        await self._apply_cfg(
            cfg,
            self._autonomy,
            yaml_patch=executor_yaml_patch(cfg),
            label=f"executor → {choice}",
        )

    async def _cmd_mode(self) -> None:
        status = self._status_view()
        values = [(mode, mode) for mode in status.modes]
        choice = await self._pick_option("mode", values, status.active_mode)
        if choice is None or choice == status.active_mode:
            return
        try:
            cfg = apply_mode(self._cfg, choice)
        except SettingsApplyError as exc:
            self._console.print(f"[yellow]{exc}[/yellow]")
            return
        await self._apply_cfg(
            cfg,
            self._autonomy,
            yaml_patch=executor_yaml_patch(cfg),
            label=f"mode → {choice}",
        )

    async def _cmd_policies(self) -> None:
        values = [(mode.value, mode.value) for mode in AutonomyMode]
        choice = await self._pick_option("policies", values, self._autonomy.value)
        if choice is None or choice == self._autonomy.value:
            return
        try:
            cfg, autonomy = apply_policies(self._cfg, choice)
        except SettingsApplyError as exc:
            self._console.print(f"[yellow]{exc}[/yellow]")
            return
        await self._apply_cfg(
            cfg,
            autonomy,
            yaml_patch=policies_yaml_patch(autonomy),
            label=f"policies → {autonomy.value}",
        )

    def _print_help(self) -> None:
        lines = ["[bold]слэш-команды[/bold]"]
        lines += [f"  {cmd.usage:<18} {cmd.help}" for cmd in COMMANDS]
        lines.append("")
        lines.append("[bold]клавиши[/bold]")
        lines += [f"  {key:<28} {desc}" for key, desc in _KEYS_HELP]
        lines.append("")
        lines.append("[dim]меню `/` и `@` появляется только при наборе · alt-screen нет[/dim]")
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
    allow_layout_overlap: bool = False,
) -> None:
    """Точка входа inline-режима из `svarog chat` (loop создаёт вызывающий)."""
    chat = InlineChat(
        cfg, workspace, autonomy, base_hooks, allow_layout_overlap=allow_layout_overlap
    )
    await chat.run(continue_ref=continue_ref, fork_ref=fork_ref)
