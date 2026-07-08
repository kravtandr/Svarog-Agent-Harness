"""Тесты Telegram-бота (#25) через фейковый транспорт (без сети)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import ModelsConfig
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.telegram import TelegramBot, TelegramTransport
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.runtime import orchestrator


class ScriptedProvider(ModelProvider):
    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        return self.turns.pop(0)


class FakeTransport(TelegramTransport):
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.answered: list[str] = []

    async def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        return []

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> None:
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})

    async def answer_callback(self, callback_id: str) -> None:
        self.answered.append(callback_id)


def _write_config(ws: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "svarog.db"
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {db_path}\n",
        encoding="utf-8",
    )


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayService:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    return GatewayService(load_config(project_dir=ws), ws)


def _patch_provider(monkeypatch: pytest.MonkeyPatch, turns: list[CompletionResult]) -> None:
    provider = ScriptedProvider(turns)

    def fake_default_provider(models_cfg: ModelsConfig, store: object = None) -> ModelProvider:
        return provider

    monkeypatch.setattr(orchestrator, "default_provider", fake_default_provider)


def _message(user_id: int, text: str) -> dict[str, Any]:
    return {"message": {"chat": {"id": 100}, "from": {"id": user_id}, "text": text}}


async def test_unauthorized_user_denied(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(monkeypatch, [CompletionResult(content="ok", usage=Usage(1, 1))])
    tx = FakeTransport()
    bot = TelegramBot(service, tx, allowed_users={42})
    await bot.handle_update(_message(999, "сделай что-то"))
    assert tx.sent == [{"chat_id": 100, "text": "⛔ Доступ запрещён.", "reply_markup": None}]


async def test_task_runs_and_final_answer_sent(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(
        monkeypatch,
        [
            CompletionResult(
                content="",
                tool_calls=(
                    ToolCallRequest(
                        id="c1",
                        name="write_file",
                        arguments_json='{"path": "n.txt", "content": "hi"}',
                    ),
                ),
                usage=Usage(10, 5),
            ),
            CompletionResult(content="Готово, файл создан", usage=Usage(10, 5)),
        ],
    )
    tx = FakeTransport()
    bot = TelegramBot(service, tx, allowed_users={42})
    await bot.handle_update(_message(42, "создай файл"))

    final = tx.sent[-1]["text"]
    assert "Готово, файл создан" in final
    assert "write_file" in final  # список использованных tools
    assert (service.workspace / "n.txt").read_text(encoding="utf-8") == "hi"


async def test_approval_buttons_then_callback_resumes(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(
        monkeypatch,
        [
            CompletionResult(
                content="",
                tool_calls=(
                    ToolCallRequest(
                        id="c1",
                        name="request_approval",
                        arguments_json='{"action": "рискованный шаг", "details": "rm -rf build"}',
                    ),
                ),
                usage=Usage(10, 5),
            ),
            CompletionResult(content="Продолжил после approval", usage=Usage(10, 5)),
        ],
    )
    tx = FakeTransport()
    bot = TelegramBot(service, tx, allowed_users={42})
    await bot.handle_update(_message(42, "рискованная задача"))

    approval_msg = tx.sent[-1]
    assert approval_msg["reply_markup"] is not None
    buttons = approval_msg["reply_markup"]["inline_keyboard"][0]
    approve_data = buttons[0]["callback_data"]
    assert approve_data.startswith("approve:")

    await bot.handle_update(
        {
            "callback_query": {
                "id": "cb1",
                "from": {"id": 42},
                "message": {"chat": {"id": 100}},
                "data": approve_data,
            }
        }
    )
    assert tx.answered == ["cb1"]
    assert any("Продолжил после approval" in m["text"] for m in tx.sent)
