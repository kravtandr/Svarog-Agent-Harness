"""Детерминированный Verifier (§6.11): checks после run'а.

Запускает тесты/линтеры (команды из конфига + skill-specific checks) в
sandbox и secret scan рабочего дерева. Детерминированные проверки имеют
приоритет над самооценкой агента: провал check'а — это провал, независимо
от финального ответа модели (LLM-as-judge — пост-MVP, ADR-0008).
"""

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from svarog_harness.config.schema import CheckSpec
from svarog_harness.sandbox.base import ExecutionEnvironment
from svarog_harness.secrets import scan_files
from svarog_harness.skills.models import Skill
from svarog_harness.storage.models import CheckStatus

# Каталоги, которые не сканируем на секреты (артефакты, vcs, зависимости).
_SCAN_SKIP_DIRS = frozenset({".git", ".svarog", "__pycache__", "node_modules", ".venv"})
_CHECK_TIMEOUT_SEC = 300.0
_MAX_OUTPUT_CHARS = 8_000


@dataclass(frozen=True)
class CheckOutcome:
    name: str
    status: CheckStatus
    output: str

    @property
    def passed(self) -> bool:
        return self.status is CheckStatus.PASSED


def skill_checks(skills: list[Skill], loaded_names: set[str]) -> list[CheckSpec]:
    """Skill-specific checks для скиллов, загруженных в этом run'е (§6.4)."""
    specs: list[CheckSpec] = []
    for skill in skills:
        if skill.name not in loaded_names:
            continue
        for i, command in enumerate(skill.metadata.checks):
            specs.append(CheckSpec(name=f"skill:{skill.name}:{i}", command=command))
    return specs


class Verifier:
    def __init__(self, environment: ExecutionEnvironment, workspace: Path) -> None:
        self._env = environment
        self._workspace = workspace

    async def run(
        self,
        checks: list[CheckSpec],
        *,
        secret_scan: bool = True,
        known_values: frozenset[str] = frozenset(),
    ) -> list[CheckOutcome]:
        outcomes: list[CheckOutcome] = []
        for spec in checks:
            outcomes.append(await self._run_command(spec))
        if secret_scan:
            outcomes.append(self._secret_scan(known_values))
        return outcomes

    async def _run_command(self, spec: CheckSpec) -> CheckOutcome:
        try:
            result = await self._env.execute(spec.command, timeout_sec=_CHECK_TIMEOUT_SEC)
        except Exception as exc:  # среда упала — check не смог выполниться
            return CheckOutcome(spec.name, CheckStatus.ERROR, str(exc))
        if result.timed_out:
            return CheckOutcome(spec.name, CheckStatus.ERROR, "check превысил timeout")
        output = _truncate((result.stdout + result.stderr).strip())
        status = CheckStatus.PASSED if result.exit_code == 0 else CheckStatus.FAILED
        return CheckOutcome(spec.name, status, output)

    def _secret_scan(self, known_values: frozenset[str]) -> CheckOutcome:
        files: dict[str, str] = {}
        for path in self._workspace.rglob("*"):
            if not path.is_file() or self._skipped(path):
                continue
            try:
                files[str(path.relative_to(self._workspace))] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # бинарные/нечитаемые файлы пропускаем
        findings = scan_files(files, known_values=known_values)
        if not findings:
            return CheckOutcome("secret-scan", CheckStatus.PASSED, "секретов не найдено")
        report = "\n".join(f"{f.path}:{f.line} [{f.rule}] {f.excerpt}" for f in findings)
        return CheckOutcome("secret-scan", CheckStatus.FAILED, _truncate(report))

    def _skipped(self, path: Path) -> bool:
        rel = path.relative_to(self._workspace)
        return any(part in _SCAN_SKIP_DIRS for part in rel.parts) or fnmatch.fnmatch(
            rel.name, "*.pyc"
        )


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return f"{text[:_MAX_OUTPUT_CHARS]}\n… [обрезано, всего {len(text)} символов]"
