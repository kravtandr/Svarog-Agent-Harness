"""Bash tool (§6.5): исполнение shell-команд в workspace.

В M1 единственная среда — local-trusted (§17): команда выполняется на
хосте без изоляции, это явный режим для доверенных локальных задач.
Docker sandbox подключается в M2 (ADR-0002); tool уже сейчас декларирует
sandbox_requirement=REQUIRED, чтобы policy/sandbox могли это enforced'ить.

Timeout обрабатывается внутри execute убийством группы процессов:
внешний asyncio.wait_for лишь отменил бы communicate(), оставив
процесс работать.
"""

import asyncio
import contextlib
import os
import signal
from pathlib import Path

from pydantic import BaseModel, Field

from svarog_harness.tools.base import (
    RiskLevel,
    SandboxRequirement,
    Tool,
    ToolResult,
    truncate_text,
)

_MAX_STREAM_CHARS = 20_000


class BashArgs(BaseModel):
    command: str = Field(description="Shell-команда; рабочая директория — корень workspace")


class BashTool(Tool[BashArgs]):
    name = "bash"
    description = "Выполнить shell-команду в workspace; возвращает exit code, stdout и stderr"
    risk_level = RiskLevel.MEDIUM
    sandbox_requirement = SandboxRequirement.REQUIRED
    args_model = BashArgs

    def __init__(self, workspace: Path, command_timeout_sec: float = 120.0) -> None:
        self.workspace = workspace
        self._command_timeout = command_timeout_sec
        # Запас для базового wait_for: сам не сработает, убийство группы — внутри execute.
        self.timeout_sec = command_timeout_sec + 10

    async def execute(self, args: BashArgs) -> ToolResult:
        proc = await asyncio.create_subprocess_shell(
            args.command,
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # своя группа процессов, чтобы убить и потомков
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=self._command_timeout
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            await proc.wait()
            return ToolResult.failure(
                f"команда превысила timeout {self._command_timeout}s и была убита: {args.command}"
            )

        stdout = truncate_text(stdout_bytes.decode(errors="replace"), _MAX_STREAM_CHARS)
        stderr = truncate_text(stderr_bytes.decode(errors="replace"), _MAX_STREAM_CHARS)
        parts = [f"exit code: {proc.returncode}"]
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        output = "\n".join(parts)
        if proc.returncode != 0:
            return ToolResult(ok=False, output=output, error=f"exit code {proc.returncode}")
        return ToolResult.success(output)
