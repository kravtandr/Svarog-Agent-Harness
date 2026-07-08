"""Tool read_skill (§6.4): загрузка полного содержимого скилла on-demand.

Скиллы известны из скана при старте run; в контекст попадают только
карточки. read_skill возвращает тело SKILL.md целиком и логирует факт
загрузки (SkillLoad) — сырьё для Skill Curator (ADR-0009). Логирование
идёт через callback, чтобы tool не зависел от слоя storage.
"""

from collections.abc import Callable

from pydantic import BaseModel, Field

from svarog_harness.skills.models import Skill
from svarog_harness.skills.proposal import SKILL_FILE, SkillProposalRequest, validate_proposal
from svarog_harness.tools.base import RiskLevel, Tool, ToolResult

# (skill_name, skill_version) — loop подписывается для записи SkillLoad.
SkillLoadCallback = Callable[[str, str | None], None]
# proposal кладётся в sink; orchestrator материализует его в ветке после run.
SkillProposalCallback = Callable[[SkillProposalRequest], None]


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


class CreateSkillProposalArgs(BaseModel):
    name: str = Field(description="Имя скилла (каталог в skills/)")
    skill_md: str = Field(
        description=(
            "Полное содержимое SKILL.md с YAML-frontmatter; обязательны "
            "name/description/version/risk и provenance: agent (§18.1)"
        )
    )
    action: str = Field(default="create", description="create | update")
    note: str = Field(default="", description="Пояснение для ревьюера, зачем нужен скилл")
    files: dict[str, str] = Field(
        default_factory=dict,
        description="Дополнительные файлы скилла: путь относительно каталога → содержимое",
    )


class CreateSkillProposalTool(Tool[CreateSkillProposalArgs]):
    name = "create_skill_proposal"
    action_type = "skill.propose"
    description = (
        "Предложить новый или обновлённый скилл на review (Flow B): "
        "прямые изменения skills/ запрещены, только через proposal (§18)"
    )
    risk_level = RiskLevel.LOW
    args_model = CreateSkillProposalArgs

    def __init__(self, on_propose: SkillProposalCallback) -> None:
        self._on_propose = on_propose

    async def execute(self, args: CreateSkillProposalArgs) -> ToolResult:
        files = {SKILL_FILE: args.skill_md, **args.files}
        request = SkillProposalRequest(
            skill_name=args.name, action=args.action, files=files, note=args.note
        )
        errors = validate_proposal(request)
        if errors:
            return ToolResult.failure("proposal невалиден:\n" + "\n".join(f"- {e}" for e in errors))
        self._on_propose(request)
        return ToolResult.success(
            f"skill proposal '{args.name}' принят и поставлен на review (§18); "
            f"человек проверит diff и решит merge/reject"
        )
