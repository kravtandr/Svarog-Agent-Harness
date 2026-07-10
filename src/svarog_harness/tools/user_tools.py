"""Tool ask_user (§6.5): уточняющий вопрос человеку с таймаутом.

Агент вызывает его, когда не может продолжать без уточнения. Run уходит в
waiting_approval (как approval, ADR-0005): вопрос показывается человеку в
любом интерфейсе, ответ приходит текстом и возвращается модели как результат
вызова. Ключевое отличие от request_approval — таймаут: если ответа нет к
дедлайну, при resume вопрос считается истёкшим и агент получает сигнал
«ответа нет, действуй по своему усмотрению» (§6.10), а не зависает навсегда.

execute() в штатном потоке не вызывается — loop возвращает результат вопроса
(ответ или истечение) из _consume_question до исполнения tool.
"""

from pydantic import BaseModel, Field

from svarog_harness.tools.base import RiskLevel, Tool, ToolResult

ASK_USER_TOOL_NAME = "ask_user"


class AskUserArgs(BaseModel):
    question: str = Field(description="Вопрос человеку — коротко и конкретно")
    timeout_sec: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Сколько секунд ждать ответа, прежде чем продолжить по своему усмотрению; "
            "по умолчанию — runtime.ask_user_timeout_sec"
        ),
    )


class AskUserTool(Tool[AskUserArgs]):
    name = ASK_USER_TOOL_NAME
    action_type = "user.question"
    description = (
        "Задать человеку уточняющий вопрос и дождаться ответа. Работа продолжится "
        "после ответа; при отсутствии ответа к таймауту — продолжай по своему усмотрению"
    )
    risk_level = RiskLevel.MEDIUM
    args_model = AskUserArgs

    async def execute(self, args: AskUserArgs) -> ToolResult:
        # Недостижимо в штатном потоке: ответ подставляет loop до вызова execute.
        return ToolResult.success("ответ пользователя не получен")
