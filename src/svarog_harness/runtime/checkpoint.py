"""Сериализация состояния agent loop для checkpoint/resume (ADR-0005).

Checkpoint пишется после каждого шага loop; `pending_tool_calls` — это
write-ahead: вызовы, зафиксированные до исполнения. При resume они
доисполняются первыми (граница идемпотентности — между исполнением tool
и записью следующего checkpoint: на ней вызов может повториться).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from svarog_harness.llm.provider import ChatMessage, ToolCallRequest


@dataclass
class LoopState:
    """Возобновляемое состояние run'а — ровно то, что хранит Checkpoint.state."""

    workspace: Path
    messages: list[ChatMessage]
    task: str = ""
    iterations: int = 0  # всего за run (стоп-кран max_iterations)
    tokens_used: int = 0
    cost_usd: float = 0.0
    pending_tool_calls: tuple[ToolCallRequest, ...] = ()
    # Итераций с последнего refuel; при пороге контекст сбрасывается (§6.10).
    iterations_since_refuel: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "messages": [_message_to_dict(m) for m in self.messages],
            "task": self.task,
            "iterations": self.iterations,
            "tokens_used": self.tokens_used,
            "cost_usd": self.cost_usd,
            "pending_tool_calls": [_call_to_dict(c) for c in self.pending_tool_calls],
            "iterations_since_refuel": self.iterations_since_refuel,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LoopState":
        return cls(
            workspace=Path(raw["workspace"]),
            messages=[_message_from_dict(m) for m in raw["messages"]],
            task=raw.get("task", ""),
            iterations=raw["iterations"],
            tokens_used=raw["tokens_used"],
            cost_usd=raw["cost_usd"],
            pending_tool_calls=tuple(_call_from_dict(c) for c in raw["pending_tool_calls"]),
            iterations_since_refuel=raw.get("iterations_since_refuel", raw["iterations"]),
        )


def _call_to_dict(call: ToolCallRequest) -> dict[str, Any]:
    return {"id": call.id, "name": call.name, "arguments_json": call.arguments_json}


def _call_from_dict(raw: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(id=raw["id"], name=raw["name"], arguments_json=raw["arguments_json"])


def _message_to_dict(message: ChatMessage) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_calls": [_call_to_dict(c) for c in message.tool_calls],
        "tool_call_id": message.tool_call_id,
    }


def _message_from_dict(raw: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        role=raw["role"],
        content=raw["content"],
        tool_calls=tuple(_call_from_dict(c) for c in raw["tool_calls"]),
        tool_call_id=raw["tool_call_id"],
    )
