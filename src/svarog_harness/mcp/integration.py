"""Подключение MCP-серверов и сборка MCP tools (§9).

`connect_mcp_servers` поднимает соединения по конфигу и делает discovery;
`build_mcp_tools` превращает обнаруженные спецификации в MCPTool для реестра.
SDK `mcp` импортируется лениво (опциональная зависимость): без него модуль
импортируется, а попытка реально подключиться даёт понятную ошибку.
"""

from contextlib import suppress
from typing import Any

from svarog_harness.config.schema import MCPConfig, MCPServerConfig
from svarog_harness.mcp.models import MCPBackend, MCPError, MCPToolSpec
from svarog_harness.mcp.tool import MCPTool
from svarog_harness.secrets import SecretStore, injected_env
from svarog_harness.tools.base import RiskLevel

_RISK = {
    "low": RiskLevel.LOW,
    "medium": RiskLevel.MEDIUM,
    "high": RiskLevel.HIGH,
    "critical": RiskLevel.CRITICAL,
}


class StdioMCPBackend(MCPBackend):
    """MCP-сервер по stdio-транспорту (subprocess), поверх SDK `mcp`."""

    def __init__(
        self, server_name: str, cfg: MCPServerConfig, store: SecretStore | None = None
    ) -> None:
        self.server_name = server_name
        self.risk = _RISK[cfg.risk]
        self._cfg = cfg
        self._store = store
        self._stack: Any = None
        self._session: Any = None
        self._specs: list[MCPToolSpec] = []

    async def connect(self) -> None:
        try:
            from contextlib import AsyncExitStack

            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover - зависит от установки extra
            raise MCPError(
                "MCP требует опциональную зависимость: uv pip install 'svarog-harness[mcp]'"
            ) from exc

        env = injected_env(self._store, self._cfg.env_refs) if self._store is not None else {}
        params = StdioServerParameters(
            command=self._cfg.command, args=list(self._cfg.args), env=env or None
        )
        # Всё после создания стека — под защитой: сервер мог стартовать, но не
        # ответить на initialize. Тогда транспорт уже открыт, а стек не закрыт
        # никем: его разберёт сборщик мусора — в чужой задаче, — и anyio даст
        # «Attempted to exit cancel scope in a different task». Хуже того,
        # осиротевшая task group отменяет задачу вызывающего: случайный GC
        # способен убить чужой живой HTTP-запрос. Подключение либо целиком
        # удалось, либо не оставило следов.
        self._stack = AsyncExitStack()
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            listed = await self._session.list_tools()
            self._specs = [
                MCPToolSpec(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=dict(tool.inputSchema or {"type": "object"}),
                )
                for tool in listed.tools
            ]
        except BaseException:
            # BaseException, а не Exception: отмена по таймауту — штатный путь
            # этой ветки (gateway даёт connect 20 секунд), и убрать за собой
            # надо именно в ней.
            await self.close()
            raise

    def specs(self) -> list[MCPToolSpec]:
        return self._specs

    async def call(self, tool: str, arguments: dict[str, Any]) -> str:
        if self._session is None:
            raise MCPError(f"MCP-сервер '{self.server_name}' не подключён")
        try:
            result = await self._session.call_tool(tool, arguments)
        except Exception as exc:  # любой сбой SDK превращаем в ошибку tool'а
            raise MCPError(f"вызов MCP tool '{tool}' не удался: {exc}") from exc
        return _render_content(result)

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._session = None


def _render_content(result: Any) -> str:
    """Собрать текстовые блоки ответа MCP tool в строку."""
    blocks = getattr(result, "content", None) or []
    parts = [getattr(block, "text", "") for block in blocks if getattr(block, "text", "")]
    text = "\n".join(p for p in parts if p)
    if getattr(result, "isError", False):
        return f"ошибка MCP tool: {text or '(без текста)'}"
    return text or "(пустой ответ MCP tool)"


async def connect_mcp_servers(cfg: MCPConfig, store: SecretStore | None = None) -> list[MCPBackend]:
    """Подключить все MCP-серверы из конфигурации и сделать discovery (§9)."""
    backends: list[MCPBackend] = []
    for name, server in cfg.servers.items():
        backend = StdioMCPBackend(name, server, store)
        try:
            await backend.connect()
        except BaseException:
            # Вызывающие присваивают результат одной операцией (gateway и
            # orchestrator.py:557), поэтому при исключении у них на руках нет
            # ничего, что можно закрыть, — уже поднятые серверы остались бы
            # висеть процессами. Список отдаётся целиком или не отдаётся.
            for started in backends:
                with suppress(Exception):
                    await started.close()
            raise
        backends.append(backend)
    return backends


def build_mcp_tools(backends: list[MCPBackend]) -> list[MCPTool]:
    """Развернуть обнаруженные MCP-инструменты в MCPTool для реестра."""
    return [MCPTool(backend, spec) for backend in backends for spec in backend.specs()]
