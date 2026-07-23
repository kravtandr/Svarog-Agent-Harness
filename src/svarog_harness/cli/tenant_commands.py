"""Команды `svarog tenant`: тенанты мультиарендного режима (ADR-0012/0014).

Вынесено из main.py: общие хелперы берутся из cli/_shared.py, поэтому модуль
не импортирует main.py и цикла не создаёт.
"""

import asyncio
from typing import Annotated

import typer

from svarog_harness.cli._shared import console, load_config_or_exit

tenant_app = typer.Typer(
    help="Тенанты мультиарендного режима (ADR-0012/0014).", no_args_is_help=True
)


@tenant_app.command("create")
def tenant_create(
    tenant_id: Annotated[str, typer.Argument(help="Идентификатор тенанта")],
    role: Annotated[
        str, typer.Option("--role", help="superuser (хост) | standard (только sandbox)")
    ] = "standard",
) -> None:
    """Завести тенанта: home-дерево, git-репозитории, БД и bearer-token (ADR-0014)."""
    from svarog_harness.config.paths import registry_path
    from svarog_harness.config.schema import TenantRole
    from svarog_harness.tenant import TenantExistsError, TenantRegistry, provision_tenant

    try:
        parsed_role = TenantRole(role)
    except ValueError:
        console.print(f"[red]неизвестная роль '{role}'[/red] — ожидается superuser | standard")
        raise typer.Exit(code=1) from None
    cfg = load_config_or_exit()
    registry = TenantRegistry(registry_path(cfg))
    try:
        result = asyncio.run(provision_tenant(cfg, registry, tenant_id, parsed_role))
    except TenantExistsError:
        console.print(f"[red]тенант '{tenant_id}' уже существует[/red]")
        raise typer.Exit(code=1) from None
    console.print(
        f"[green]тенант создан:[/green] {result.tenant_id} ({parsed_role.value})\n"
        f"[dim]home: {result.home}[/dim]\n"
        f"[bold]bearer-token (сохраните — показывается один раз):[/bold]\n{result.token}"
    )


@tenant_app.command("list")
def tenant_list() -> None:
    """Список зарегистрированных тенантов."""
    from svarog_harness.config.paths import registry_path
    from svarog_harness.tenant import TenantRegistry

    cfg = load_config_or_exit()
    tenants = TenantRegistry(registry_path(cfg)).list_tenants()
    if not tenants:
        console.print("тенантов нет — заведите: svarog tenant create <id>")
        return
    for rec in tenants:
        console.print(
            f"[bold]{rec.tenant_id}[/bold] · {rec.role.value} · "
            f"principals: {len(rec.principals)} · {rec.created_at}"
        )


@tenant_app.command("add-principal")
def tenant_add_principal(
    tenant_id: Annotated[str, typer.Argument(help="Идентификатор тенанта")],
    principal: Annotated[str, typer.Argument(help="Principal, напр. telegram:123456789")],
) -> None:
    """Привязать principal (telegram:<id> / gateway:<token>) к тенанту."""
    from svarog_harness.config.paths import registry_path
    from svarog_harness.tenant import (
        PrincipalConflictError,
        TenantRegistry,
        TenantRegistryError,
    )

    cfg = load_config_or_exit()
    registry = TenantRegistry(registry_path(cfg))
    try:
        registry.add_principal(tenant_id, principal)
    except PrincipalConflictError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    except TenantRegistryError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]principal привязан:[/green] {principal} → {tenant_id}")


@tenant_app.command("token")
def tenant_token(
    tenant_id: Annotated[str, typer.Argument(help="Идентификатор тенанта")],
    rotate: Annotated[
        bool, typer.Option("--rotate", help="Выпустить новый токен, отозвав прежний")
    ] = False,
) -> None:
    """Показать текущий или (с --rotate) выпустить новый gateway-token тенанта."""
    from svarog_harness.config.paths import registry_path
    from svarog_harness.tenant import (
        TenantRegistry,
        current_token,
        rotate_token,
    )

    cfg = load_config_or_exit()
    registry = TenantRegistry(registry_path(cfg))
    if registry.get(tenant_id) is None:
        console.print(f"[red]нет тенанта '{tenant_id}'[/red]")
        raise typer.Exit(code=1)
    if rotate:
        token = rotate_token(cfg, registry, tenant_id)
        console.print(f"[green]новый bearer-token[/green] для {tenant_id}:\n{token}")
        return
    saved = current_token(cfg, tenant_id)
    if not saved:
        console.print(
            f"[yellow]у {tenant_id} нет сохранённого токена[/yellow] — "
            f"svarog tenant token {tenant_id} --rotate"
        )
        raise typer.Exit(code=1)
    console.print(f"[bold]bearer-token[/bold] {tenant_id}:\n{saved}")
