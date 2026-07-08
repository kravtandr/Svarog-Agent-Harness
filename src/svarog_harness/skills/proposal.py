"""Skill proposal (Flow B, §18): заявка агента на новый/обновлённый скилл.

Агент не пишет в активные skills напрямую (policy deny) — он формирует
proposal: набор файлов скилла (обязателен SKILL.md) + примечание. Заявка
валидируется (frontmatter, provenance) и материализуется в отдельной ветке
skills-репозитория; merge — только после человеческого review (§18, ADR-0003).
"""

from dataclasses import dataclass, field
from typing import Any, Self

from svarog_harness.skills.loader import SkillMetadataError, parse_metadata

# Единственный обязательный файл скилла (§7).
SKILL_FILE = "SKILL.md"


@dataclass(frozen=True)
class SkillProposalRequest:
    skill_name: str
    action: str  # create | update
    # Пути относительно каталога скилла → содержимое; обязателен SKILL.md.
    files: dict[str, str] = field(default_factory=dict)
    note: str = ""
    source_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "action": self.action,
            "files": self.files,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source_run_id: str | None = None) -> Self:
        return cls(
            skill_name=str(data["skill_name"]),
            action=str(data.get("action", "create")),
            files={str(k): str(v) for k, v in data.get("files", {}).items()},
            note=str(data.get("note", "")),
            source_run_id=source_run_id,
        )


def validate_proposal(request: SkillProposalRequest) -> list[str]:
    """Проверить заявку до материализации; пустой список — валидна.

    Провенанс agent-created обязателен: curator работает только с такими
    скиллами (§18.1, ADR-0009), поэтому proposal без него мог бы обойти
    кураторские инварианты.
    """
    errors: list[str] = []
    if SKILL_FILE not in request.files:
        return [f"proposal должен содержать {SKILL_FILE}"]
    try:
        metadata, _ = parse_metadata(request.skill_name, request.files[SKILL_FILE])
    except SkillMetadataError as exc:
        return [f"{SKILL_FILE} невалиден: {exc}"]
    if metadata.name != request.skill_name:
        errors.append(
            f"name в {SKILL_FILE} ('{metadata.name}') не совпадает с '{request.skill_name}'"
        )
    if metadata.provenance != "agent":
        errors.append(
            f"agent-created скилл должен иметь 'provenance: agent' в {SKILL_FILE} (§18.1)"
        )
    if request.action not in ("create", "update"):
        errors.append(f"action должен быть create|update, получен '{request.action}'")
    return errors
