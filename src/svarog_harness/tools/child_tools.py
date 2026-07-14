"""Tool spawn_child_run (ADR-0015 фаза 3): дочерний run для подзадачи.

Дочерний run — обычный `Run` с `parent_run_id`, своим checkpoint'ом
(ADR-0005) и снимком security-конфига (§0.4); изоляция — отдельный
git-worktree. Сам tool — тонкая обёртка: логика spawn'а живёт в
orchestrator (там доступны recorder, sandbox и конфиг), tool получает
её callback'ом, чтобы не зависеть от слоёв runtime/storage.

Бюджеты ребёнка клампятся ВНИЗ к родительским (как autonomy/role):
запросить больше, чем у родителя, нельзя. Ребёнку не выдаётся
spawn_child_run — глубина дерева ограничена одним уровнем (защита от
рекурсивного размножения runs; снятие лимита — осознанное решение позже).
"""

from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, Field

from svarog_harness.tools.base import RiskLevel, Tool, ToolResult

# Callback оркестратора: исполнить дочерний run и вернуть текст результата
# для модели; ошибки поднимаются как ToolError.
SpawnChildCallback = Callable[["SpawnChildRunArgs"], Awaitable[str]]

# Дочерний run включает LLM-вызовы и может идти долго; timeout самого tool
# ставим с большим запасом — реальные стоп-краны у ребёнка свои (бюджеты).
_SPAWN_TIMEOUT_SEC = 3600.0


class SpawnChildRunArgs(BaseModel):
    task: str = Field(description="Самодостаточная формулировка подзадачи для дочернего run'а")
    max_iterations: int | None = Field(
        default=None, ge=1, description="Бюджет итераций ребёнка (клампится вниз к родительскому)"
    )
    max_tokens: int | None = Field(
        default=None, ge=1, description="Бюджет токенов ребёнка (клампится вниз к родительскому)"
    )
    max_cost_usd: float | None = Field(
        default=None, gt=0, description="Бюджет стоимости ребёнка (клампится вниз к родительскому)"
    )
    executor: Literal["native", "external"] = Field(
        default="native",
        description=(
            "Исполнитель подзадачи: native — вложенный agent loop; external — "
            "делегировать внешнему кодинг-агенту (ADR-0016 фаза 3.5; доступно "
            "только если в конфиге настроен executor.external, иначе tool-ошибка)"
        ),
    )


class SpawnChildRunTool(Tool[SpawnChildRunArgs]):
    name = "spawn_child_run"
    action_type = "run.spawn_child"
    description = (
        "Запустить дочерний run для самодостаточной подзадачи в изолированном "
        "git-worktree и вернуть его результат; работа ребёнка остаётся на его ветке"
    )
    risk_level = RiskLevel.MEDIUM
    timeout_sec = _SPAWN_TIMEOUT_SEC
    args_model = SpawnChildRunArgs

    def __init__(self, on_spawn: SpawnChildCallback) -> None:
        self._on_spawn = on_spawn

    async def execute(self, args: SpawnChildRunArgs) -> ToolResult:
        return ToolResult.success(await self._on_spawn(args))
