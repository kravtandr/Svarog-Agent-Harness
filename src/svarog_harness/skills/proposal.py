"""Skill proposal (Flow B, §18): заявка агента на новый/обновлённый скилл.

Агент не пишет в активные skills напрямую (policy deny) — он формирует
proposal: набор файлов скилла (обязателен SKILL.md) + примечание. Заявка
валидируется (frontmatter, provenance) и материализуется в отдельной ветке
skills-репозитория; merge — только после человеческого review (§18, ADR-0003).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from svarog_harness.paths import PathTraversalError, safe_join
from svarog_harness.skills.loader import SkillMetadataError, parse_metadata

# Единственный обязательный файл скилла (§7).
SKILL_FILE = "SKILL.md"

# Имя каталога скилла: без `..`, `/`, начальной точки и прочих traversal-трюков.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


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
    # Path-traversal (ADR-0015 §0.1): ни имя скилла, ни ключи files не должны
    # уводить запись за пределы каталога скилла в skills-репозитории. Проверяем
    # до материализации — ошибка возвращается модели, запись на хост не идёт.
    if not _SKILL_NAME_RE.match(request.skill_name) or ".." in request.skill_name:
        errors.append(
            f"недопустимое имя скилла '{request.skill_name}': ожидается "
            f"^[a-z0-9][a-z0-9._-]*$ без '..'/'/'/начальной точки"
        )
    else:
        skill_root = Path("/__skill__") / request.skill_name
        for rel in request.files:
            try:
                safe_join(skill_root, rel)
            except PathTraversalError:
                errors.append(f"путь файла скилла выходит за пределы каталога: '{rel}'")
    if SKILL_FILE not in request.files:
        return [f"proposal должен содержать {SKILL_FILE}", *errors]
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
