"""Bash tool (§6.5): исполнение shell-команд в ExecutionEnvironment.

Гарантии изоляции — свойство переданной среды (ADR-0002): docker дает
слой 1, local-trusted (§17) работает без гарантий. Timeout команды
обеспечивает сама среда; tool лишь форматирует результат для модели.
"""

from pydantic import BaseModel, Field

from svarog_harness.sandbox.base import ExecutionEnvironment
from svarog_harness.tools.base import (
    RiskLevel,
    SandboxRequirement,
    Tool,
    ToolResult,
    truncate_text,
)

# Жёсткий потолок захвата — защита памяти процесса, не экономия контекста:
# backpressure (персистенция + усечение) применяет loop (ADR-0015 §1.2).
_MAX_STREAM_CHARS = 1_000_000


class BashArgs(BaseModel):
    command: str = Field(description="Shell-команда; рабочая директория — корень workspace")


class BashTool(Tool[BashArgs]):
    name = "bash"
    action_type = "bash.exec"
    description = "Выполнить shell-команду в workspace; возвращает exit code, stdout и stderr"
    risk_level = RiskLevel.MEDIUM
    sandbox_requirement = SandboxRequirement.REQUIRED
    args_model = BashArgs

    def __init__(
        self, environment: ExecutionEnvironment, command_timeout_sec: float = 120.0
    ) -> None:
        self._env = environment
        self._command_timeout = command_timeout_sec
        # Запас для базового wait_for: за timeout команды отвечает среда.
        self.timeout_sec = command_timeout_sec + 30

    async def execute(self, args: BashArgs) -> ToolResult:
        result = await self._env.execute(args.command, timeout_sec=self._command_timeout)
        if result.timed_out:
            return ToolResult.failure(
                f"команда превысила timeout {self._command_timeout}s и была убита: {args.command}"
            )

        stdout = truncate_text(result.stdout, _MAX_STREAM_CHARS)
        stderr = truncate_text(result.stderr, _MAX_STREAM_CHARS)
        parts = [f"exit code: {result.exit_code}"]
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        output = "\n".join(parts)
        if result.exit_code != 0:
            return ToolResult(ok=False, output=output, error=f"exit code {result.exit_code}")
        return ToolResult.success(output)
