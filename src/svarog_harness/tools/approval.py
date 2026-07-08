"""Tool request_approval (§6.5, ADR-0010): явный запрос подтверждения человека.

Агент вызывает его сам, когда считает действие рискованным. risk_level =
critical, поэтому Policy Engine дает require_approval в любом режиме
автономии: run уходит в waiting_approval, человек решает через
`svarog approvals` или интерактивный промпт, resume продолжает работу.
execute() достигается только после одобрения (loop потребляет решение).
"""

from pydantic import BaseModel, Field

from svarog_harness.tools.base import RiskLevel, Tool, ToolResult


class RequestApprovalArgs(BaseModel):
    action: str = Field(description="Что агент собирается сделать (коротко)")
    details: str = Field(
        default="", description="Фактическая команда, diff или детали — то, что увидит человек"
    )


class RequestApprovalTool(Tool[RequestApprovalArgs]):
    name = "request_approval"
    action_type = "approval.request"
    description = (
        "Запросить у человека подтверждение рискованного действия; "
        "выполнение продолжится после решения"
    )
    risk_level = RiskLevel.CRITICAL  # → require_approval в любом режиме (§3.6)
    args_model = RequestApprovalArgs

    async def execute(self, args: RequestApprovalArgs) -> ToolResult:
        return ToolResult.success(f"пользователь одобрил: {args.action}")
