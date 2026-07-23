"""Команды `svarog secrets`: имена секретов и запись значений в файл store."""

from typing import Annotated

import typer

from svarog_harness.cli._shared import console, load_config_or_exit
from svarog_harness.secrets import FileSecretStore, default_secret_store

secrets_app = typer.Typer(
    help="SecretStore: имена секретов (значения не показываются).", no_args_is_help=True
)


@secrets_app.command("list")
def secrets_list() -> None:
    """Показать имена секретов из файла store (значения не раскрываются)."""
    cfg = load_config_or_exit()
    names = default_secret_store(cfg.secrets.path).names()
    if not names:
        console.print("секретов в файле store нет (env-секреты по именам не перечисляются)")
        return
    for name in names:
        console.print(name)


@secrets_app.command("set")
def secrets_set(
    name: Annotated[str, typer.Argument(help="Имя секрета (например PROVIDER_API_KEY)")],
    value: Annotated[str, typer.Option("--value", prompt=True, hide_input=True, help="Значение")],
) -> None:
    """Записать секрет в файл store (права 0600). Значение вводится скрыто."""
    cfg = load_config_or_exit()
    if cfg.secrets.path is None:
        console.print("[red]secrets.path не задан в конфигурации[/red]")
        raise typer.Exit(code=1)
    store = FileSecretStore(cfg.secrets.path.expanduser())
    store.set(name, value)
    console.print(f"[green]секрет '{name}' сохранён[/green] в {cfg.secrets.path}")
