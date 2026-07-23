"""Команда `svarog mcp list`: discovery инструментов MCP-серверов (§9)."""

import asyncio
import contextlib

import typer

from svarog_harness.cli._shared import console, load_config_or_exit
from svarog_harness.mcp import MCPError, build_mcp_tools, connect_mcp_servers
from svarog_harness.secrets import default_secret_store

mcp_app = typer.Typer(help="MCP-серверы: discovery инструментов (§9).", no_args_is_help=True)


@mcp_app.command("list")
def mcp_list() -> None:
    """Подключить настроенные MCP-серверы и показать обнаруженные инструменты."""
    cfg = load_config_or_exit()
    if not cfg.mcp.servers:
        console.print("MCP-серверы не настроены (секция mcp.servers в svarog.yaml)")
        return
    store = default_secret_store(cfg.secrets.path)

    async def discover() -> None:
        backends = await connect_mcp_servers(cfg.mcp, store)
        try:
            tools = build_mcp_tools(backends)
            if not tools:
                console.print("инструменты на MCP-серверах не обнаружены")
                return
            for tool in tools:
                console.print(
                    f"[cyan]{tool.name}[/cyan] [dim](risk={tool.risk_level.value}, "
                    f"action={tool.action_type}, по умолчанию approval)[/dim]"
                )
                if tool.description:
                    console.print(f"  {tool.description}")
        finally:
            for backend in backends:
                with contextlib.suppress(Exception):
                    await backend.close()

    try:
        asyncio.run(discover())
    except MCPError as exc:
        console.print(f"[red]MCP: {exc}[/red]")
        raise typer.Exit(code=1) from None
