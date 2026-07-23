"""Control-plane bridge внешнего агента (ADR-0016 §4/§6/§7, фазы 2-3).

Два endpoint'а поверх bridge-сервера:

* `/svarog/mcp` — MCP-сервер Svarog (JSON-RPC поверх HTTP): «обратные»
  инструменты remember / read_memory / read_skill / create_skill_proposal /
  ask_user / request_approval. Память идёт в очередь single-writer'а,
  proposals — в sink Flow B, всё под тем же governance, что у нативного loop.
* `/svarog/hook` — PreToolUse-мост (tier 2): каждый вызов инструмента агента
  прогоняется через Policy Engine c замороженным на старте run снапшотом
  (ADR-0010); require_approval ждёт grace period и при отсутствии решения
  запрашивает suspend всего run (§7) — контейнер не живёт часами.

Decision cache (§7): решение человека хранится в Approval с отпечатком
вызова (tool + канонический hash аргументов); после resume агент ретраит
действие, hook находит решение по отпечатку и пропускает без второго
approval.

БД: у executor'а своя AsyncSession; control-слой на каждый запрос открывает
короткую сессию через db_action (with_db) — конкурентного доступа к одной
сессии нет.
"""

import asyncio
import contextlib
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.policy.engine import PolicyAction, PolicyEngine
from svarog_harness.runtime.bridge import ControlHandler
from svarog_harness.runtime.self_docs import resolve_docs_root
from svarog_harness.secrets.redaction import redact
from svarog_harness.skills import Skill
from svarog_harness.skills.proposal import SkillProposalRequest
from svarog_harness.storage.models import Approval, ApprovalStatus, Run, utcnow
from svarog_harness.tools.base import RiskLevel, Tool
from svarog_harness.tools.docs_tools import ReadSvarogDocsTool
from svarog_harness.tools.memory_tools import ReadMemoryTool, RememberTool
from svarog_harness.tools.skill_tools import CreateSkillProposalTool, ReadSkillTool
from svarog_harness.tools.user_tools import question_options
from svarog_harness.trace.recorder import TraceRecorder

# Версия MCP-протокола, которую сервер подтверждает клиенту.
_MCP_PROTOCOL = "2024-11-05"
# Период опроса решения approval во время grace-ожидания.
_POLL_INTERVAL_SEC = 1.0

# Маппинг встроенных инструментов агентов → типизированные операции Policy
# Engine (те же action_type, что у нативных tools). Незнакомый инструмент —
# fail-closed HIGH risk с именем как есть.
_HOOK_ACTIONS: dict[str, tuple[str, RiskLevel]] = {
    # Риски зеркалят нативные tools (shell/file_tools); bash-эвристики
    # эскалируют MEDIUM → HIGH внутри общего конвейера policy.
    "Bash": ("bash.exec", RiskLevel.MEDIUM),
    "Write": ("file.write", RiskLevel.MEDIUM),
    "Edit": ("file.write", RiskLevel.MEDIUM),
    "MultiEdit": ("file.write", RiskLevel.MEDIUM),
    "NotebookEdit": ("file.write", RiskLevel.MEDIUM),
    "Read": ("file.read", RiskLevel.LOW),
    "Glob": ("file.search", RiskLevel.LOW),
    "Grep": ("file.search", RiskLevel.LOW),
    "WebFetch": ("net.fetch", RiskLevel.HIGH),
    "WebSearch": ("net.fetch", RiskLevel.HIGH),
}

# Ключ отпечатка вызова в payload approval'а (decision cache, §7).
FINGERPRINT_KEY = "fingerprint"

DbAction = Callable[..., Awaitable[Any]]


def call_fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
    """Отпечаток вызова: tool + канонический JSON аргументов (§7)."""
    canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{tool_name}\n{canonical}".encode()).hexdigest()


class BridgeControl:
    """Состояние control-plane одного run внешнего агента."""

    def __init__(
        self,
        *,
        db_action: DbAction,
        policy: PolicyEngine,
        memory_dir: Path | None,
        skills: list[Skill],
        proposal_sink: list[SkillProposalRequest],
        secret_values: frozenset[str] = frozenset(),
        approval_grace_sec: float = 120.0,
        ask_user_timeout_sec: int = 3600,
        on_notify: Callable[[str, str], None] | None = None,
        on_approval_prompt: Callable[[Approval], Awaitable[None]] | None = None,
        self_docs: bool = True,
    ) -> None:
        self._db_action = db_action
        self._policy = policy
        self._memory_dir = memory_dir
        self._skills = skills
        self._proposal_sink = proposal_sink
        self._self_docs = self_docs
        self._secret_values = secret_values
        self._grace_sec = approval_grace_sec
        self._ask_user_timeout_sec = ask_user_timeout_sec
        self._on_notify = on_notify
        # Живой промпт решения (§7): интерфейс сам записывает решение в БД,
        # poll-цикл _human_gate подхватывает его без suspend.
        self._on_approval_prompt = on_approval_prompt
        self.run_id: str | None = None
        # Suspend-сигнал (§7): executor отменяет стрим и переводит run в
        # waiting_approval; reason — что именно ждём.
        self.suspend = asyncio.Event()
        self.suspend_reason: str = ""
        # remember/read_skill кладут сюда синхронно (callback tool'а);
        # _flush_side_effects переносит в БД короткой сессией.
        self._pending_memory: list[dict[str, Any]] = []
        self._pending_skill_loads: list[tuple[str, str | None]] = []
        self._tools = self._build_tools()

    # --- регистрация ---

    def handlers(self) -> dict[str, ControlHandler]:
        return {"mcp": self.handle_mcp, "hook": self.handle_hook}

    def set_run(self, run: Run) -> None:
        self.run_id = run.id

    def _build_tools(self) -> dict[str, Tool[Any]]:
        tools: dict[str, Tool[Any]] = {}
        if self._memory_dir is not None:
            tools["remember"] = RememberTool(
                on_enqueue=lambda req: self._pending_memory.append(req.to_dict()),
                memory_dir=self._memory_dir,
            )
            tools["read_memory"] = ReadMemoryTool(self._memory_dir)
        if self._skills:
            tools["read_skill"] = ReadSkillTool(self._skills, on_load=self._on_skill_load)
        tools["create_skill_proposal"] = CreateSkillProposalTool(
            on_propose=self._proposal_sink.append
        )
        # Документация самого Svarog: агент отвечает про систему по источнику,
        # а не по претрейну. Недоступный docs-root — фича молча выключается.
        if self._self_docs and resolve_docs_root() is not None:
            tools["read_svarog_docs"] = ReadSvarogDocsTool()
        return tools

    def _on_skill_load(self, name: str, version: str | None) -> None:
        self._pending_skill_loads.append((name, version))

    # --- MCP-сервер (§4) ---

    async def handle_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        method = payload.get("method")
        msg_id = payload.get("id")
        match method:
            case "initialize":
                params = payload.get("params") or {}
                return _rpc_result(
                    msg_id,
                    {
                        "protocolVersion": params.get("protocolVersion") or _MCP_PROTOCOL,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "svarog", "version": "0.1.0"},
                    },
                )
            case "notifications/initialized" | "notifications/cancelled":
                return {"_status": 202}
            case "ping":
                return _rpc_result(msg_id, {})
            case "tools/list":
                tools = [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.args_model.model_json_schema(),
                    }
                    for tool in self._tools.values()
                ]
                tools.append(_ASK_USER_DEF)
                tools.append(_REQUEST_APPROVAL_DEF)
                return _rpc_result(msg_id, {"tools": tools})
            case "tools/call":
                params = payload.get("params") or {}
                name = str(params.get("name", ""))
                arguments = params.get("arguments")
                arguments = arguments if isinstance(arguments, dict) else {}
                text, is_error = await self._call_tool(name, arguments)
                return _rpc_result(
                    msg_id,
                    {
                        "content": [{"type": "text", "text": text}],
                        "isError": is_error,
                    },
                )
            case _:
                return _rpc_error(msg_id, -32601, f"метод не поддерживается: {method}")

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if name == "ask_user":
            payload: dict[str, Any] = {
                "question": str(arguments.get("question", "")),
                "deadline": (utcnow() + timedelta(seconds=self._ask_user_timeout_sec)).isoformat(),
            }
            options = question_options(arguments)
            if options:
                payload["options"] = options
            return await self._human_gate(
                action_type="user.question",
                payload=payload,
                pending_reason="ask_user: ждём ответа человека",
                approved_prefix="ответ пользователя: ",
            )
        if name == "request_approval":
            return await self._human_gate(
                action_type="approval.request",
                payload={
                    "action": str(arguments.get("action", "")),
                    "details": str(arguments.get("details", "")),
                },
                pending_reason="request_approval: ждём решения человека",
                approved_prefix="пользователь одобрил: ",
            )
        tool = self._tools.get(name)
        if tool is None:
            return f"неизвестный MCP-tool: {name}", True
        result = await tool.call(arguments)
        await self._flush_side_effects()
        text = redact(
            result.output if result.ok else (result.error or "ошибка"), self._secret_values
        )
        if self._on_notify is not None:
            self._on_notify("bridge.mcp", f"{name}: {'ok' if result.ok else 'ошибка'}")
        return text, not result.ok

    async def _flush_side_effects(self) -> None:
        """Перенести заявки remember/read_skill в БД (короткая сессия)."""
        memory = list(self._pending_memory)
        self._pending_memory.clear()
        loads = list(self._pending_skill_loads)
        self._pending_skill_loads.clear()
        if not memory and not loads:
            return
        run_id = self.run_id

        async def action(db: AsyncSession) -> None:
            recorder = TraceRecorder(db)
            run = await recorder.get_run(run_id) if run_id is not None else None
            if run is None:
                return
            for change in memory:
                await recorder.enqueue_memory_change(run, change)
            for skill_name, version in loads:
                await recorder.log_skill_load(
                    run, skill_name=skill_name, skill_version=version, source="mcp"
                )

        await self._db_action(action)

    # --- hook-мост (§6) и человеческие гейты (§7) ---

    async def handle_hook(self, payload: dict[str, Any]) -> dict[str, Any]:
        tool_name = str(payload.get("tool_name", ""))
        tool_input = payload.get("tool_input")
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        action_type, risk = _HOOK_ACTIONS.get(tool_name, (f"agent.{tool_name}", RiskLevel.HIGH))
        if tool_name.startswith("mcp__svarog"):
            # Собственные инструменты Svarog проверяются на MCP-слое.
            return {"decision": "allow", "reason": ""}
        decision = self._policy.evaluate_agent_tool(action_type, tool_input, risk=risk)
        if self._on_notify is not None and decision.action is PolicyAction.NOTIFY:
            self._on_notify("policy.notify", f"{action_type}: {decision.reason}")
        match decision.action:
            case PolicyAction.ALLOW | PolicyAction.NOTIFY:
                return {"decision": "allow", "reason": ""}
            case PolicyAction.DENY:
                return {"decision": "deny", "reason": decision.reason}
            case PolicyAction.REQUIRE_APPROVAL:
                text, is_error = await self._human_gate(
                    action_type=decision.action_type,
                    payload={"tool_name": tool_name, "tool_input": tool_input},
                    pending_reason=f"approval для {action_type}: ждём решения человека",
                    approved_prefix="",
                    fingerprint=call_fingerprint(tool_name, tool_input),
                )
                if is_error:
                    return {"decision": "deny", "reason": text}
                return {"decision": "allow", "reason": text}
        return {"decision": "deny", "reason": "policy: неизвестное решение (fail-closed)"}

    async def _human_gate(
        self,
        *,
        action_type: str,
        payload: dict[str, Any],
        pending_reason: str,
        approved_prefix: str,
        fingerprint: str | None = None,
    ) -> tuple[str, bool]:
        """Общий путь approval/ask_user (§7): decision cache → grace → suspend."""
        if fingerprint is not None:
            cached = await self._find_by_fingerprint(fingerprint)
            if cached is not None and cached.status is ApprovalStatus.APPROVED:
                return "решение из decision cache: одобрено", False
            if cached is not None and cached.status is ApprovalStatus.DENIED:
                return f"отклонено человеком: {cached.reason or 'без причины'}", True
            approval = cached
        else:
            approval = None
        if approval is None:
            if fingerprint is not None:
                payload = {**payload, FINGERPRINT_KEY: fingerprint}
            payload = {**payload, "call_id": f"bridge-{uuid.uuid4().hex[:12]}"}
            approval = await self._create_approval(action_type, payload)
            if approval is None:
                return "bridge: run ещё не зарегистрирован", True
            if self._on_notify is not None:
                self._on_notify("approval.requested", f"{action_type} → {approval.id[:8]}")
        prompt_task: asyncio.Task[None] | None = None
        if self._on_approval_prompt is not None:
            # Fire-and-forget: промпт блокируется на stdin в worker-потоке и
            # пишет решение в БД сам; здесь его не ждём — poll ниже увидит
            # решение, а grace-таймаут сработает и при молчании человека.
            prompt_task = asyncio.ensure_future(self._on_approval_prompt(approval))
        deadline = asyncio.get_running_loop().time() + self._grace_sec
        approval_id = approval.id
        try:
            while asyncio.get_running_loop().time() < deadline:
                status, reason = await self._approval_status(approval_id)
                if status is ApprovalStatus.APPROVED:
                    return f"{approved_prefix}{reason or ''}".strip() or "одобрено", False
                if status is ApprovalStatus.DENIED:
                    return f"отклонено человеком: {reason or 'без причины'}", True
                await asyncio.sleep(_POLL_INTERVAL_SEC)
        finally:
            if prompt_task is not None:
                # Поток stdin отменой не прервать; cancel лишь отвязывает
                # task — позднее решение попадёт в decision cache (§7).
                # Сбой промпта (EOF/закрытый stdin) гейт не роняет: решение
                # остаётся доступным асинхронно через `svarog approvals`.
                prompt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await prompt_task
        # Grace истёк — просим executor приостановить run (§7); агенту
        # возвращаем понятный отказ с маркером.
        self.suspend_reason = f"{pending_reason} (approval {approval_id[:8]})"
        self.suspend.set()
        return (
            f"SVAROG-PENDING {approval_id[:8]}: {pending_reason}; run будет приостановлен, "
            "продолжение — svarog resume после решения",
            True,
        )

    async def _create_approval(self, action_type: str, payload: dict[str, Any]) -> Approval | None:
        run_id = self.run_id
        if run_id is None:
            return None

        async def action(db: AsyncSession) -> Approval | None:
            recorder = TraceRecorder(db)
            run = await recorder.get_run(run_id)
            if run is None:
                return None
            return await recorder.create_approval(run, action_type=action_type, payload=payload)

        return cast(Approval | None, await self._db_action(action))

    async def _approval_status(self, approval_id: str) -> tuple[ApprovalStatus | None, str | None]:
        async def action(db: AsyncSession) -> tuple[ApprovalStatus | None, str | None]:
            recorder = TraceRecorder(db)
            try:
                approval = await recorder.find_approval_by_prefix(approval_id)
            except Exception:
                return None, None
            return approval.status, approval.reason

        return cast(tuple[ApprovalStatus | None, str | None], await self._db_action(action))

    async def _find_by_fingerprint(self, fingerprint: str) -> Approval | None:
        run_id = self.run_id
        if run_id is None:
            return None

        async def action(db: AsyncSession) -> Approval | None:
            result = await db.execute(select(Approval).where(Approval.run_id == run_id))
            for approval in result.scalars():
                if approval.payload.get(FINGERPRINT_KEY) == fingerprint:
                    return approval
            return None

        return cast(Approval | None, await self._db_action(action))


# Определения человеческих гейтов для tools/list (не Tool-классы: их
# семантика на bridge своя — grace + suspend, §7).
_ASK_USER_DEF = {
    "name": "ask_user",
    "description": (
        "Задать человеку уточняющий вопрос и дождаться ответа; если ответа нет, "
        "run приостановится до ответа"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Вопрос человеку"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "2–5 коротких вариантов ответа, если выбор конечен: человек "
                    "выберет один стрелочками или ответит свободным текстом"
                ),
            },
        },
        "required": ["question"],
    },
}
_REQUEST_APPROVAL_DEF = {
    "name": "request_approval",
    "description": "Запросить у человека подтверждение рискованного действия",
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "Что собираешься сделать"},
            "details": {"type": "string", "description": "Команда/diff/детали"},
        },
        "required": ["action"],
    },
}


def _rpc_result(msg_id: object, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
