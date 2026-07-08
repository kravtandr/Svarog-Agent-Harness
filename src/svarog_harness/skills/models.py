"""Модель скилла и его метаданных (§7)."""

from dataclasses import dataclass, field
from pathlib import Path

from svarog_harness.tools.base import RiskLevel


@dataclass(frozen=True)
class SkillMetadata:
    """Разобранный frontmatter SKILL.md. Обязательны name/description/version/risk."""

    name: str
    description: str
    version: str
    risk: RiskLevel
    allowed_tools: tuple[str, ...] = ()
    requires_approval: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    # Provenance для curator (ADR-0009): human — вне зоны автоматических переходов.
    provenance: str = "human"


@dataclass(frozen=True)
class Skill:
    """Скилл на диске: метаданные, путь к SKILL.md и его тело."""

    metadata: SkillMetadata
    path: Path  # каталог скилла
    body: str  # текст SKILL.md без frontmatter

    @property
    def name(self) -> str:
        return self.metadata.name

    def card(self) -> str:
        """Краткая карточка для контекста (progressive disclosure, §3.4)."""
        meta = self.metadata
        parts = [f"- {meta.name} (v{meta.version}, risk={meta.risk.value}): {meta.description}"]
        if meta.allowed_tools:
            parts.append(f"  tools: {', '.join(meta.allowed_tools)}")
        return "\n".join(parts)


@dataclass(frozen=True)
class SkillError:
    """Скилл, который не удалось загрузить: путь и причина (для skills check)."""

    path: Path
    reason: str


@dataclass
class SkillScan:
    skills: list[Skill] = field(default_factory=list)
    errors: list[SkillError] = field(default_factory=list)
