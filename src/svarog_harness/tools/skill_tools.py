"""Tool read_skill (§6.4): загрузка полного содержимого скилла on-demand.

Скиллы известны из скана при старте run; в контекст попадают только
карточки. read_skill возвращает тело SKILL.md целиком и логирует факт
загрузки (SkillLoad) — сырьё для Skill Curator (ADR-0009). Логирование
идёт через callback, чтобы tool не зависел от слоя storage.
"""

from collections.abc import Callable

from pydantic import BaseModel, Field

from svarog_harness.skills.models import Skill
from svarog_harness.tools.base import RiskLevel, Tool, ToolResult

# (skill_name, skill_version) — loop подписывается для записи SkillLoad.
SkillLoadCallback = Callable[[str, str | None], None]


class ReadSkillArgs(BaseModel):
    name: str = Field(description="Имя скилла из списка доступных skills")


class ReadSkillTool(Tool[ReadSkillArgs]):
    name = "read_skill"
    action_type = "skill.read"
    description = "Загрузить полную инструкцию скилла (SKILL.md) по его имени"
    risk_level = RiskLevel.LOW
    args_model = ReadSkillArgs

    def __init__(self, skills: list[Skill], *, on_load: SkillLoadCallback | None = None) -> None:
        self._skills = {skill.name: skill for skill in skills}
        self._on_load = on_load

    async def execute(self, args: ReadSkillArgs) -> ToolResult:
        skill = self._skills.get(args.name)
        if skill is None:
            available = ", ".join(sorted(self._skills)) or "нет"
            return ToolResult.failure(f"скилл '{args.name}' не найден (доступны: {available})")
        if self._on_load is not None:
            self._on_load(skill.name, skill.metadata.version)
        header = f"# SKILL: {skill.name} (v{skill.metadata.version})"
        return ToolResult.success(f"{header}\n\n{skill.body}")
