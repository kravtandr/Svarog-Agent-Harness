"""Memory Curator: детерминированный аудит здоровья памяти-wiki (ADR-0011, §18.1).

Аналог слоя 1 skill-curator'а (ADR-0009), но структурный: у страниц памяти
нет usage-телеметрии, поэтому аудит ищет структурные проблемы — осиротевшие,
битые, устаревшие и пустые страницы. Ничего не мутирует и не блокирует run'ы
(подсистемы memory-proposals пока нет) — только отчёт для человека. Семантический
слой (противоречия, дубли проектов на LLM) — отдельный будущий шаг.
"""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from svarog_harness.common.frontmatter import split_frontmatter
from svarog_harness.memory.project_page import validate_project_page

# Файлы-автоген (ведёт код) и служебные — из аудита исключаются.
_AUTOGEN_FILES = frozenset({"index.md", "log.md"})

# Виды находок.
KIND_ORPHAN = "orphan"  # папка проекта без overview.md
KIND_INVALID = "invalid"  # overview.md не проходит контракт
KIND_STALE = "stale"  # status active, но updated давно
KIND_EMPTY = "empty"  # пустой .md-файл


@dataclass(frozen=True)
class MemoryFinding:
    kind: str
    path: str
    detail: str


@dataclass
class MemoryAuditReport:
    findings: list[MemoryFinding] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = ["# Memory curation report", ""]
        if not self.findings:
            lines.append("Находок нет — память в порядке.")
            return "\n".join(lines) + "\n"
        for finding in self.findings:
            lines.append(f"## {finding.kind}: {finding.path}")
            lines.append(finding.detail)
            lines.append("")
        return "\n".join(lines).rstrip("\n") + "\n"


def _parse_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _audit_projects(memory_dir: Path, *, stale_after_days: int, today: date) -> list[MemoryFinding]:
    root = memory_dir / "projects"
    if not root.is_dir():
        return []
    findings: list[MemoryFinding] = []
    for slug_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        overview = slug_dir / "overview.md"
        rel = f"projects/{slug_dir.name}/overview.md"
        if not overview.is_file():
            findings.append(
                MemoryFinding(
                    KIND_ORPHAN, f"projects/{slug_dir.name}/", "папка проекта без overview.md"
                )
            )
            continue
        content = overview.read_text(encoding="utf-8")
        error = validate_project_page(content, expected_slug=slug_dir.name)
        if error is not None:
            findings.append(MemoryFinding(KIND_INVALID, rel, error))
            continue
        frontmatter, _ = split_frontmatter(content)
        status = str(frontmatter.get("status", "")).strip().lower()
        updated = _parse_date(frontmatter.get("updated"))
        if status == "active" and updated is not None:
            age = (today - updated).days
            if age >= stale_after_days:
                findings.append(
                    MemoryFinding(
                        KIND_STALE, rel, f"status active, но обновлялась {age} дн. назад"
                    )
                )
    return findings


def _audit_empty(memory_dir: Path) -> list[MemoryFinding]:
    findings: list[MemoryFinding] = []
    for md in sorted(memory_dir.rglob("*.md")):
        rel = md.relative_to(memory_dir)
        if str(rel) in _AUTOGEN_FILES:
            continue
        try:
            if not md.read_text(encoding="utf-8").strip():
                findings.append(MemoryFinding(KIND_EMPTY, str(rel), "пустой файл"))
        except OSError:
            continue
    return findings


def audit_memory(
    memory_dir: Path, *, stale_after_days: int, today: date | None = None
) -> MemoryAuditReport:
    """Структурный аудит памяти-wiki. Детерминированный, только чтение."""
    today = today or date.today()
    findings = _audit_projects(memory_dir, stale_after_days=stale_after_days, today=today)
    findings += _audit_empty(memory_dir)
    return MemoryAuditReport(findings=findings)
