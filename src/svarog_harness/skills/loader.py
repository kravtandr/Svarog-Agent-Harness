"""Skill Loader (§6.4): сканирование skills/, парсинг SKILL.md, skill cards.

Полное содержимое скилла загружается только on-demand через read_skill
(progressive disclosure, §3.4); в контекст попадают лишь карточки.
"""

from pathlib import Path

from svarog_harness.skills.frontmatter import first_body_paragraph, split_frontmatter
from svarog_harness.skills.models import Skill, SkillError, SkillMetadata, SkillScan
from svarog_harness.tools.base import RiskLevel

_REQUIRED_FIELDS = ("name", "description", "version", "risk")


class SkillMetadataError(Exception):
    """SKILL.md не проходит валидацию обязательных полей (§7)."""


def _as_str_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


def parse_metadata(default_name: str, content: str) -> tuple[SkillMetadata, str]:
    """Разобрать SKILL.md в метаданные и тело; SkillMetadataError при проблемах."""
    frontmatter, body = split_frontmatter(content)
    if not frontmatter:
        raise SkillMetadataError("отсутствует YAML-frontmatter (--- в начале файла)")

    missing = [f for f in _REQUIRED_FIELDS if not frontmatter.get(f)]
    if missing:
        raise SkillMetadataError(f"нет обязательных полей: {', '.join(missing)}")

    risk_raw = str(frontmatter["risk"]).strip().lower()
    try:
        risk = RiskLevel(risk_raw)
    except ValueError:
        allowed = ", ".join(level.value for level in RiskLevel)
        raise SkillMetadataError(f"недопустимый risk '{risk_raw}' (допустимо: {allowed})") from None

    name = str(frontmatter["name"]).strip() or default_name
    description = str(frontmatter["description"]).strip() or first_body_paragraph(body)
    provenance = str(frontmatter.get("provenance", "human")).strip().lower() or "human"

    metadata = SkillMetadata(
        name=name,
        description=description,
        version=str(frontmatter["version"]).strip(),
        risk=risk,
        allowed_tools=_as_str_tuple(frontmatter.get("allowed_tools")),
        requires_approval=_as_str_tuple(frontmatter.get("requires_approval")),
        checks=_as_str_tuple(frontmatter.get("checks")),
        tags=_as_str_tuple(frontmatter.get("tags")),
        provenance=provenance,
    )
    return metadata, body


def load_skill(skill_dir: Path) -> Skill:
    """Загрузить один скилл из каталога с SKILL.md."""
    skill_md = skill_dir / "SKILL.md"
    content = skill_md.read_text(encoding="utf-8")
    metadata, body = parse_metadata(skill_dir.name, content)
    return Skill(metadata=metadata, path=skill_dir, body=body)


def scan_skills(paths: list[Path]) -> SkillScan:
    """Просканировать каталоги skills/; каждый подкаталог с SKILL.md — скилл.

    Дубликаты имён игнорируются с записью в errors: первый по порядку
    выигрывает (project-каталоги идут раньше user-каталогов по конвенции).
    """
    scan = SkillScan()
    seen: set[str] = set()
    for root in paths:
        expanded = root.expanduser()
        if not expanded.is_dir():
            continue
        for skill_md in sorted(expanded.glob("*/SKILL.md")):
            skill_dir = skill_md.parent
            try:
                skill = load_skill(skill_dir)
            except (OSError, SkillMetadataError) as exc:
                scan.errors.append(SkillError(path=skill_dir, reason=str(exc)))
                continue
            if skill.name in seen:
                scan.errors.append(
                    SkillError(path=skill_dir, reason=f"дубликат имени скилла '{skill.name}'")
                )
                continue
            seen.add(skill.name)
            scan.skills.append(skill)
    return scan


def skill_cards(skills: list[Skill]) -> str:
    """Собрать секцию skill cards для контекста."""
    if not skills:
        return ""
    lines = ["Доступные skills (загрузи полностью через read_skill перед использованием):"]
    lines.extend(skill.card() for skill in skills)
    return "\n".join(lines)
