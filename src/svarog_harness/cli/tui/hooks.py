"""Адаптер RunHooks → Textual-сообщения (ADR-0018).

Все хуки, кроме approval, стреляют на loop'е приложения (run исполняется
async-worker'ом Textual) — `post_message` дешёв и потокобезопасен.
`on_approval_requested` приходит из worker-потока bridge-гейта (§7):
модалка показывается через `call_from_thread`, поток ждёт вердикта на
`threading.Event`, решение пишется в БД с того же потока — poll гейта
подхватывает его, UI-loop не блокируется.
"""

from typing import TYPE_CHECKING

from textual.message import Message

from svarog_harness.cli.chat_engine import record_gate_answer, record_gate_decision
from svarog_harness.config.schema import SvarogConfig
from svarog_harness.runtime.orchestrator import RunHooks
from svarog_harness.skills.proposal_manager import SkillProposalManager
from svarog_harness.storage.models import Approval

if TYPE_CHECKING:
    from svarog_harness.cli.tui.app import SvarogChatApp


class TextDelta(Message):
    """Кусок стрима ответа агента (нативно — токены, external — блоки)."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class ToolCalled(Message):
    def __init__(self, name: str, args: dict[str, object]) -> None:
        super().__init__()
        self.name = name
        self.args = args


class ProgressUpdated(Message):
    def __init__(self, iterations: int, tokens: int, cost: float, context_ratio: float) -> None:
        super().__init__()
        self.iterations = iterations
        self.tokens = tokens
        self.cost = cost
        self.context_ratio = context_ratio


class PanelEvent(Message):
    """Событие для панели: kind ∈ {check, commit, memory, notify, …}."""

    def __init__(self, kind: str, text: str, *, ok: bool | None = None) -> None:
        super().__init__()
        self.kind = kind
        self.text = text
        self.ok = ok


def _gate_prompt(app: "SvarogChatApp", cfg: SvarogConfig, approval: Approval) -> None:
    """Живой гейт external-пути: модалка + запись решения в БД (worker-поток)."""
    decision = app.prompt_gate_from_thread(approval)
    if approval.action_type == "user.question":
        record_gate_answer(cfg, approval.id, decision.answer or "", answered_by="chat")
        return
    record_gate_decision(
        cfg,
        approval.id,
        approved=decision.approved,
        reason=decision.reason,
        decided_by="chat",
    )


def build_tui_hooks(app: "SvarogChatApp", cfg: SvarogConfig) -> RunHooks:
    """RunHooks, транслирующие ход прогона в сообщения приложения.

    Текстовое наполнение панельных событий повторяет `_console_hooks`
    plain-режима (§21) — та же информация, другой транспорт.
    """

    def post(message: Message) -> None:
        app.post_message(message)

    return RunHooks(
        on_skill_skipped=lambda name, reason: post(
            PanelEvent("skill", f"skill пропущен ({name}): {reason}", ok=False)
        ),
        on_workspace_prep=lambda prep: post(
            PanelEvent(
                "workspace",
                f"ветка {prep.branch}{' (после pull)' if prep.pulled else ''}"
                if prep.is_git and prep.branch
                else (prep.note or "workspace готов"),
            )
        ),
        on_recovered=lambda run: post(
            PanelEvent(
                "recovered",
                f"run {run.id[:8]} был прерван — suspended (svarog resume {run.id[:8]})",
                ok=False,
            )
        ),
        on_progress=lambda iterations, tokens, cost, ratio: post(
            ProgressUpdated(iterations, tokens, cost, ratio)
        ),
        on_text_delta=lambda delta: post(TextDelta(delta)),
        on_tool_call=lambda name, args: post(ToolCalled(name, args)),
        on_notify=lambda name, reason: post(
            PanelEvent("notify", f"⚡ {name} — {reason}", ok=False)
        ),
        on_check=lambda check: post(
            PanelEvent("check", f"check {check.status.value} {check.name}", ok=check.passed)
        ),
        on_verify_failed=lambda count: post(
            PanelEvent(
                "verify",
                f"verifier: {count} проверок не прошли — результат нельзя считать корректным",
                ok=False,
            )
        ),
        on_commit=lambda sha, branch, needs_push: post(
            PanelEvent(
                "commit",
                f"workspace закоммичен ({sha}) на {branch}"
                + (f"; push вручную: svarog push {branch}" if needs_push else ""),
                ok=True,
            )
        ),
        on_commit_blocked=lambda msg: post(
            PanelEvent("commit", f"коммит заблокирован secret scan: {msg}", ok=False)
        ),
        on_memory=lambda sha, error: post(
            PanelEvent("memory", f"память: заявка отклонена — {error}", ok=False)
            if error
            else PanelEvent("memory", f"память обновлена ({sha})", ok=True)
        ),
        on_proposal=lambda proposal: post(
            PanelEvent(
                "proposal",
                f"skill proposal {proposal.skill_name} → ветка {proposal.branch} "
                f"(review: svarog skills proposals show {proposal.id[:8]})"
                if proposal.status.value == "pending"
                else f"skill proposal {proposal.skill_name}: {proposal.status.value} — "
                + "; ".join(SkillProposalManager.validation_messages(proposal)),
                ok=proposal.status.value == "pending",
            )
        ),
        on_approval_requested=lambda approval: _gate_prompt(app, cfg, approval),
    )
