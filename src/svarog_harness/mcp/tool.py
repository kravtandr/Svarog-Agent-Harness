"""MCPTool: обёртка MCP-инструмента в интерфейс Tool (§9).

Имя, риск и схема аргументов известны только после discovery, поэтому
задаются на инстансе. action_type — `mcp.<server>.<tool>`, что даёт Policy
Engine префикс `mcp.` для правила «по умолчанию require_approval» (§9).
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from svarog_harness.llm.provider import ToolDefinition
from svarog_harness.mcp.models import MCPBackend, MCPError, MCPToolSpec
from svarog_harness.tools.base import Tool, ToolResult


class _MCPArgs(BaseModel):
    # Схема MCP tool произвольна — принимаем любые поля и передаём как есть.
    model_config = ConfigDict(extra="allow")


class MCPTool(Tool[_MCPArgs]):
    args_model = _MCPArgs

    def __init__(self, backend: MCPBackend, spec: MCPToolSpec) -> None:
        self._backend = backend
        self._spec = spec
        self.name = f"mcp__{backend.server_name}__{spec.name}"
        self.description = spec.description or f"MCP tool {spec.name}@{backend.server_name}"
        self.action_type = f"mcp.{backend.server_name}.{spec.name}"
        self.risk_level = backend.risk

    def definition(self) -> ToolDefinition:
        schema = self._spec.input_schema or {"type": "object"}
        return ToolDefinition(name=self.name, description=self.description, input_schema=schema)

    async def execute(self, args: _MCPArgs) -> ToolResult:
        arguments: dict[str, Any] = args.model_dump()
        try:
            output = await self._backend.call(self._spec.name, arguments)
        except MCPError as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success(output)
