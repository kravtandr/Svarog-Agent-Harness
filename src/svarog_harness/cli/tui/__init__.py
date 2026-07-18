"""Полноэкранный chat-TUI на Textual (ADR-0018).

Фронтенд поверх `ChatEngine` (cli.chat_engine): транскрипт с markdown-стримом,
персистентная история ввода, статус-бар, approval-модалки, слэш-команды,
session picker и панель событий. Plain-REPL остаётся в `cli.main` (--plain).
"""

from pathlib import Path

from svarog_harness.config.schema import AutonomyMode, SvarogConfig


def run_chat_tui(
    cfg: SvarogConfig,
    workspace: Path,
    autonomy: AutonomyMode,
    *,
    continue_ref: str | None = None,
    fork_ref: str | None = None,
) -> None:
    """Запустить TUI-чат; loop'ом владеет Textual (без asyncio.run снаружи).

    Импорт приложения — ленивый, чтобы не тянуть Textual при каждом вызове CLI.
    """
    from svarog_harness.cli.tui.app import SvarogChatApp

    app = SvarogChatApp(cfg, workspace, autonomy, continue_ref=continue_ref, fork_ref=fork_ref)
    app.run()
