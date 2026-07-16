"""Policy Engine (§6.6, ADR-0010): решение о допустимости действия агента.

Решения: allow / notify / deny / require_approval. Итог зависит от режима
автономии (§3.6), но неотключаемый critical-набор дает require_approval в
любом режиме и определяется только по типизированным операциям — bash-
эвристики могут эскалировать команду лишь до high/notify (ADR-0002).

Engine создается при старте run с уже зафиксированным режимом автономии и
правилами; конфигурация не перечитывается во время исполнения — эскалация
режима изнутри run невозможна по построению (ADR-0010, защита от prompt
injection §12).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from svarog_harness.config.schema import AutonomyMode, PoliciesConfig
from svarog_harness.policy.heuristics import detect_dangerous_command, detect_protected_push
from svarog_harness.policy.rules import PolicyRule
from svarog_harness.tools.base import RiskLevel, Tool

# Неотключаемый critical-набор (§3.6): только типизированные операции.
# Tools/компоненты этих операций появляются в M3+ (gitflow, secrets, deploy);
# набор определен сейчас, чтобы ни один из них не появился без approval.
CRITICAL_ACTIONS = frozenset(
    {
        "deploy.production",
        "service.modify",
        "secrets.reveal",
        "payments.execute",
        "data.delete_outside_workspace",
        "git.force_push",
        "git.push_protected",
        "policy.weaken",
    }
)

# Tools, изменяющие файлы, — для встроенного deny на прямые правки skills/.
_FILE_WRITE_ACTIONS = frozenset({"file.write", "file.edit"})


class PolicyAction(StrEnum):
    ALLOW = "allow"
    NOTIFY = "notify"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction
    # Типизированная операция — идет в Approval.action_type и trace.
    action_type: str
    # Эффективный риск (после возможной эскалации эвристиками).
    risk_level: RiskLevel
    reason: str


class PolicyEngine:
    """Все входные данные фиксируются в конструкторе при старте run (ADR-0010)."""

    def __init__(
        self,
        *,
        autonomy: AutonomyMode,
        policies: PoliciesConfig,
        workspace: Path,
        rules: Sequence[PolicyRule] = (),
        skills_dirs: Sequence[Path] = (),
    ) -> None:
        self._autonomy = autonomy
        self._policies = policies
        self._workspace = workspace
        self._rules = tuple(rules)
        self._skills_dirs = tuple(d.expanduser().resolve() for d in skills_dirs)

    def evaluate(self, tool: Tool[Any], arguments: dict[str, Any]) -> PolicyDecision:
        """Решение для вызова tool с данными аргументами."""
        return self._evaluate_call(tool.action_type or tool.name, tool.risk_level, arguments)

    def evaluate_agent_tool(
        self, action_type: str, arguments: dict[str, Any], *, risk: RiskLevel = RiskLevel.HIGH
    ) -> PolicyDecision:
        """Инструмент внешнего агента без Tool-инстанса (ADR-0016 §6).

        Hook-мост bridge прогоняет каждый вызов инструмента агента через
        ТОТ ЖЕ конвейер, что нативные tools: skills-deny, deny-правила
        проекта, bash-эвристики, критический набор, риск × автономия.
        Незнакомый инструмент — fail-closed HIGH.
        """
        return self._evaluate_call(action_type, risk, arguments)

    def _evaluate_call(
        self, action_type: str, risk: RiskLevel, arguments: dict[str, Any]
    ) -> PolicyDecision:
        # file_path — имя аргумента файловых инструментов внешних агентов.
        path = str(arguments.get("path", "") or arguments.get("file_path", ""))

        # 1. Встроенный deny: прямые изменения skills — только через proposals (Flow B).
        if action_type in _FILE_WRITE_ACTIONS and self._path_in_skills(path):
            return PolicyDecision(
                PolicyAction.DENY,
                action_type,
                risk,
                "прямые изменения skills/ запрещены — изменения скиллов проходят "
                "через skill proposal (§18)",
            )

        # 2. Пользовательский deny из policies/*.yaml — до любых разрешающих веток.
        rule = self._match_rule("deny", action_type, path)
        if rule is not None:
            return PolicyDecision(
                PolicyAction.DENY, action_type, risk, rule.reason or f"правило deny '{rule.match}'"
            )

        # 3. Эвристики bash — UX-эскалация риска до high, не выше (ADR-0002).
        reason = ""
        if action_type == "bash.exec" and risk in (RiskLevel.LOW, RiskLevel.MEDIUM):
            command = str(arguments.get("command", ""))
            danger = detect_dangerous_command(command) or detect_protected_push(
                command, frozenset(self._policies.protected_branches)
            )
            if danger is not None:
                risk = RiskLevel.HIGH
                reason = f"эвристика: {danger}"

        return self._decide(action_type, risk, path, reason)

    def evaluate_action(
        self, action_type: str, payload: dict[str, Any], *, risk: RiskLevel = RiskLevel.HIGH
    ) -> PolicyDecision:
        """Решение для типизированной операции вне tool-слоя (gitflow, secrets).

        `git.push` в защищенную ветку эскалируется до git.push_protected
        из critical-набора.
        """
        if action_type == "git.push" and self.is_protected_branch(str(payload.get("branch", ""))):
            return PolicyDecision(
                PolicyAction.REQUIRE_APPROVAL,
                "git.push_protected",
                RiskLevel.CRITICAL,
                f"push в защищенную ветку '{payload.get('branch')}' — critical-набор (§3.6)",
            )
        return self._decide(action_type, risk, path="", extra_reason="")

    def is_protected_branch(self, branch: str) -> bool:
        return branch in self._policies.protected_branches

    def _decide(
        self, action_type: str, risk: RiskLevel, path: str, extra_reason: str
    ) -> PolicyDecision:
        """Общий хвост: critical-набор → правила/профиль → риск × автономия."""
        # Critical: approval в любом режиме, конфигурацией не отключается.
        if action_type in CRITICAL_ACTIONS or risk is RiskLevel.CRITICAL:
            return PolicyDecision(
                PolicyAction.REQUIRE_APPROVAL,
                action_type,
                RiskLevel.CRITICAL,
                "неотключаемый critical-набор (§3.6): approval обязателен в любом режиме",
            )

        # ask_user: агент просит ввод человека — run ждёт ответа в любом режиме
        # автономии (таймаут не даёт зависнуть, §6.5).
        if action_type == "user.question":
            return PolicyDecision(
                PolicyAction.REQUIRE_APPROVAL,
                action_type,
                risk,
                "запрос ввода у пользователя (ask_user)",
            )

        rule = self._match_rule("require_approval", action_type, path)
        if rule is not None or self._match_profile("require_approval", action_type):
            reason = (rule.reason if rule and rule.reason else None) or (
                f"require_approval по правилу/профилю '{self._autonomy.value}'"
            )
            return PolicyDecision(PolicyAction.REQUIRE_APPROVAL, action_type, risk, reason)

        # HIGH-риск в supervised требует approval безусловно — до notify-правил.
        # Пользовательские rules могут только ужесточать (allow запрещён схемой,
        # см. scaffold._SECURITY_POLICY), поэтому notify не должен тихо понижать
        # то, что и так обязано быть require_approval по risk × autonomy.
        if risk is RiskLevel.HIGH and self._autonomy is AutonomyMode.SUPERVISED:
            return PolicyDecision(
                PolicyAction.REQUIRE_APPROVAL,
                action_type,
                risk,
                extra_reason or "high-риск в режиме supervised",
            )

        notify_rule = self._match_rule("notify", action_type, path)
        if notify_rule is not None or self._match_profile("notify", action_type):
            reason = (notify_rule.reason if notify_rule and notify_rule.reason else None) or (
                f"notify по правилу/профилю '{self._autonomy.value}'"
            )
            return PolicyDecision(PolicyAction.NOTIFY, action_type, risk, reason)

        # MCP-инструменты по умолчанию требуют approval, пока администратор не
        # ослабит их профилем/правилом notify (§9); risk остаётся high. Ветка
        # HIGH×supervised выше уже перехватила supervised-случай — здесь
        # остаются non-supervised режимы, где это осмысленное ослабление.
        if action_type.startswith("mcp."):
            return PolicyDecision(
                PolicyAction.REQUIRE_APPROVAL,
                action_type,
                risk,
                "MCP-инструмент по умолчанию требует approval (§9)",
            )

        if risk is RiskLevel.HIGH:
            return PolicyDecision(
                PolicyAction.NOTIFY, action_type, risk, extra_reason or "high-риск: notify"
            )

        return PolicyDecision(PolicyAction.ALLOW, action_type, risk, extra_reason)

    def _match_rule(self, decision: str, action_type: str, path: str) -> PolicyRule | None:
        for rule in self._rules:
            if rule.decision != decision or not fnmatch(action_type, rule.match):
                continue
            if rule.paths and not any(fnmatch(path, pattern) for pattern in rule.paths):
                continue
            return rule
        return None

    def _match_profile(self, kind: str, action_type: str) -> bool:
        profile = self._policies.profiles.get(self._autonomy.value)
        if profile is None:
            return False
        patterns = profile.require_approval if kind == "require_approval" else profile.notify
        return any(fnmatch(action_type, pattern) for pattern in patterns)

    def _path_in_skills(self, raw_path: str) -> bool:
        if not raw_path or not self._skills_dirs:
            return False
        resolved = (self._workspace / raw_path).resolve()
        return any(resolved.is_relative_to(d) for d in self._skills_dirs)
