"""Пользовательские policy-правила из `policies/*.yaml` проекта (§6.6).

Правила могут только ужесточать поведение (deny / require_approval /
notify) — решение `allow` в правилах запрещено схемой: ослабление политик
безопасности само по себе входит в critical-набор (§3.6). Правила
загружаются один раз при старте run и не перечитываются (ADR-0010).
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError


class PolicyRulesError(Exception):
    """Файл правил не читается или не проходит валидацию схемы."""


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # fnmatch-паттерн по action_type tool'а ("bash.*", "file.write").
    match: str
    decision: Literal["deny", "require_approval", "notify"]
    reason: str = ""
    # Опционально: fnmatch-паттерны по аргументу `path` (для файловых tools).
    paths: list[str] = []


class _PolicyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[PolicyRule]


def load_policy_rules(project_dir: Path) -> list[PolicyRule]:
    """Прочитать все `policies/*.yaml`; отсутствие каталога — пустой список."""
    policies_dir = project_dir / "policies"
    if not policies_dir.is_dir():
        return []
    rules: list[PolicyRule] = []
    for file in sorted(policies_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise PolicyRulesError(f"не удалось прочитать {file}: {exc}") from exc
        try:
            parsed = _PolicyFile.model_validate(data)
        except ValidationError as exc:
            raise PolicyRulesError(f"невалидные правила в {file}: {exc}") from exc
        rules.extend(parsed.rules)
    return rules
