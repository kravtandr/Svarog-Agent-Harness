"""MCP-интеграция (§9): подключение внешних MCP-серверов как tools.

MCP tools регистрируются в Tool Registry и проходят через Policy Engine, как
любые другие. Без явно назначенного риска MCP tool получает `risk: high` и
require_approval по умолчанию (§9, ADR-0010) — ослабляется профилем notify.
SDK `mcp` — опциональная зависимость (`svarog-harness[mcp]`); импортируется
лениво, чтобы базовая установка работала без него.
"""

from svarog_harness.mcp.integration import build_mcp_tools, connect_mcp_servers
from svarog_harness.mcp.models import MCPBackend, MCPError, MCPToolSpec
from svarog_harness.mcp.tool import MCPTool

__all__ = [
    "MCPBackend",
    "MCPError",
    "MCPTool",
    "MCPToolSpec",
    "build_mcp_tools",
    "connect_mcp_servers",
]
