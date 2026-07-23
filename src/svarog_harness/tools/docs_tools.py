"""Reverse-tool документации Svarog для внешнего агента (ADR-0016 §4).

Даёт агенту читать доку самого Svarog (README, AGENTS.md, ADR), чтобы на
вопросы пользователя о системе он отвечал по источнику, а не по претрейну.
Транспорт — MCP через bridge: файловый ro-mount не годится, `read` OpenCode
отвергает пути вне cwd (см. runtime/self_docs).
"""

from pathlib import Path

from pydantic import BaseModel, Field

from svarog_harness.runtime.self_docs import build_docs_index, read_doc, resolve_docs_root
from svarog_harness.tools.base import RiskLevel, Tool, ToolResult, truncate_text

# Потолок одного ответа: крупные ADR (0015 — 60КБ) не должны съедать контекст
# целиком. Обрезка помечается в тексте (truncate_text).
_OUTPUT_LIMIT = 40_000


class ReadSvarogDocsArgs(BaseModel):
    path: str = Field(
        default="",
        description=(
            "Документ: 'README.md', 'AGENTS.md' или 'adr/<файл>.md'. "
            "Пусто — вернуть каталог доступных документов"
        ),
    )


class ReadSvarogDocsTool(Tool[ReadSvarogDocsArgs]):
    name = "read_svarog_docs"
    action_type = "docs.read"
    description = (
        "Документация самого Svarog: команды, фичи, архитектурные решения (ADR). "
        "Без параметров — каталог документов; с path — содержимое документа. "
        "ЕДИНСТВЕННЫЙ способ её прочитать: документация НЕ лежит в workspace, "
        "glob/grep/read её не найдут"
    )
    risk_level = RiskLevel.LOW
    args_model = ReadSvarogDocsArgs

    def is_read_only(self, args: ReadSvarogDocsArgs) -> bool:
        return True

    def __init__(self, root: Path | None = None) -> None:
        # None — резолвим корень доков при вызове (боевой путь).
        self._root = root

    async def execute(self, args: ReadSvarogDocsArgs) -> ToolResult:
        root = self._root if self._root is not None else resolve_docs_root()
        if root is None:
            return ToolResult.failure("документация Svarog недоступна в этой установке")
        if not args.path.strip():
            return ToolResult.success(build_docs_index(root=root))
        try:
            body = read_doc(args.path, root=root)
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success(truncate_text(f"# {args.path}\n\n{body}", _OUTPUT_LIMIT))
