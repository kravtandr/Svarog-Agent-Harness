"""Точка входа CLI `svarog`.

Команды init/run/chat/skills/traces/approvals добавляются по мере milestones
(см. docs/first-issues.md); в M0 доступна только `version`.
"""

import typer
from rich.console import Console

from svarog_harness import __version__

app = typer.Typer(
    name="svarog",
    help="Svarog — Git-native runtime for self-hosted AI agents.",
    no_args_is_help=True,
)
console = Console()


@app.callback()
def main() -> None:
    """Svarog CLI."""


@app.command()
def version() -> None:
    """Показать версию svarog-harness."""
    console.print(f"svarog-harness {__version__}")
