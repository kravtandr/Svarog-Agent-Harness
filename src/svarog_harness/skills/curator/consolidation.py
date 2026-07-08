"""Curator слой 2: семантическая консолидация на LLM (§18.1, ADR-0009).

Дорогой слой, opt-in, работает на auxiliary-модели. Один вызов LLM над
библиотекой скиллов (без VectorBackend — анализ по описаниям/телу) возвращает
находки: дубликаты/пересечения, улучшения `description` в карточках,
кандидаты на архивацию. Слой 2 ничего не применяет сам: содержательные
изменения идут через skill proposals (Flow B), отчёт — в artifacts/ и trace.
"""

import json
from dataclasses import dataclass, field

import yaml

from svarog_harness.llm.provider import ChatMessage, ModelProvider
from svarog_harness.skills.frontmatter import split_frontmatter
from svarog_harness.skills.models import Skill

# Типы находок слоя 2.
_KINDS = ("duplicate", "improve_description", "archive")

_SYSTEM = (
    "Ты — Skill Curator: поддерживаешь здоровье библиотеки скиллов ИИ-агента. "
    "Проанализируй скиллы и предложи консолидацию. Отвечай СТРОГО одним JSON-объектом "
    "без пояснений и markdown, по схеме:\n"
    '{"findings": [{"kind": "duplicate|improve_description|archive", '
    '"skills": ["имя", ...], "detail": "кратко почему", '
    '"suggested_description": "новое описание или null"}]}\n'
    "kind=duplicate — скиллы дублируют друг друга (перечисли их в skills). "
    "kind=improve_description — карточка неточна; заполни suggested_description. "
    "kind=archive — скилл устарел/бесполезен. Если находок нет — верни пустой список."
)

# Ограничиваем тело скилла в промпте, чтобы не раздувать контекст.
_BODY_EXCERPT = 600


@dataclass(frozen=True)
class CurationFinding:
    kind: str
    skills: list[str]
    detail: str
    suggested_description: str | None = None


@dataclass
class CurationReport:
    findings: list[CurationFinding] = field(default_factory=list)
    parse_error: str | None = None

    def improvements(self) -> list[CurationFinding]:
        return [
            f
            for f in self.findings
            if f.kind == "improve_description" and f.suggested_description and f.skills
        ]

    def to_markdown(self) -> str:
        lines = ["# Skill curation report (слой 2)", ""]
        if self.parse_error:
            lines.append(f"> LLM вернул неразбираемый ответ: {self.parse_error}")
            return "\n".join(lines)
        if not self.findings:
            lines.append("Находок нет — библиотека в порядке.")
            return "\n".join(lines)
        for finding in self.findings:
            lines.append(f"## {finding.kind}: {', '.join(finding.skills)}")
            lines.append(finding.detail)
            if finding.suggested_description:
                lines.append(f"- предложенное описание: {finding.suggested_description}")
            lines.append("")
        return "\n".join(lines)


def _library_prompt(skills: list[Skill]) -> str:
    blocks = []
    for skill in skills:
        meta = skill.metadata
        body = skill.body.strip().replace("\n", " ")[:_BODY_EXCERPT]
        blocks.append(
            f"- name: {meta.name}\n  provenance: {meta.provenance}\n"
            f"  description: {meta.description}\n  body: {body}"
        )
    return "Скиллы библиотеки:\n" + "\n".join(blocks)


def _parse(content: str) -> CurationReport:
    text = content.strip()
    if text.startswith("```"):
        # Снять возможные ```json … ``` ограждения.
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return CurationReport(parse_error=str(exc))
    findings: list[CurationFinding] = []
    for raw in data.get("findings", []) if isinstance(data, dict) else []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind", ""))
        if kind not in _KINDS:
            continue
        skills = [str(s) for s in raw.get("skills", []) if s]
        suggested = raw.get("suggested_description")
        findings.append(
            CurationFinding(
                kind=kind,
                skills=skills,
                detail=str(raw.get("detail", "")),
                suggested_description=str(suggested) if suggested else None,
            )
        )
    return CurationReport(findings=findings)


async def consolidate_layer2(provider: ModelProvider, skills: list[Skill]) -> CurationReport:
    """Один вызов auxiliary-LLM над библиотекой; вернуть разобранные находки."""
    if not skills:
        return CurationReport()
    messages = [
        ChatMessage(role="system", content=_SYSTEM),
        ChatMessage(role="user", content=_library_prompt(skills)),
    ]
    result = await provider.complete(messages, [])
    return _parse(result.content)


def rewrite_description(skill: Skill, new_description: str) -> str:
    """Собрать содержимое SKILL.md с обновлённым description (для proposal)."""
    content = (skill.path / "SKILL.md").read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(content)
    frontmatter["description"] = new_description
    dumped = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{dumped}\n---\n{body}"
