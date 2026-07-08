"""Telegram-интерфейс (§10.2): задачи, streaming-updates, approval-кнопки.

Тонкий транспорт поверх `GatewayService` — без логики агента (§6.1). Bot API
опрашивается long-polling'ом (getUpdates); сообщение пользователя порождает
run, ход прогона отправляется в чат, а `waiting_approval` показывается с
inline-кнопками approve/deny (решение асинхронное, ADR-0005). Токен бота —
секрет (ADR-0006), приходит из SecretStore, а не из конфига. Доступ — только
для user-id из allowlist (§16): интернет-facing бот без allowlist опасен.

Транспорт вынесен за интерфейс `TelegramTransport`, чтобы бот тестировался
без сети (фейковый транспорт со скриптованными updates).
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import httpx

from svarog_harness.gateway.service import GatewayService

_API = "https://api.telegram.org"


class TelegramTransport(ABC):
    @abstractmethod
    async def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        """Long-polling getUpdates начиная с offset."""

    @abstractmethod
    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> None:
        """Отправить сообщение (опционально с inline-клавиатурой)."""

    @abstractmethod
    async def answer_callback(self, callback_id: str) -> None:
        """Подтвердить нажатие inline-кнопки (убрать «часики» у клиента)."""


class HttpxTelegramTransport(TelegramTransport):
    """Реальный транспорт Telegram Bot API поверх httpx."""

    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._base = f"{_API}/bot{token}"
        self._client = client if client is not None else httpx.AsyncClient(timeout=60)

    async def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        resp = await self._client.get(
            f"{self._base}/getUpdates",
            params={"offset": offset, "timeout": timeout},
            timeout=timeout + 10,
        )
        resp.raise_for_status()
        result: list[dict[str, Any]] = resp.json().get("result", [])
        return result

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        resp = await self._client.post(f"{self._base}/sendMessage", json=payload)
        resp.raise_for_status()

    async def answer_callback(self, callback_id: str) -> None:
        await self._client.post(
            f"{self._base}/answerCallbackQuery", json={"callback_query_id": callback_id}
        )


# Максимальная длина одного сообщения Telegram — 4096; режем с запасом.
_MSG_LIMIT = 3800


class TelegramBot:
    def __init__(
        self,
        service: GatewayService,
        transport: TelegramTransport,
        *,
        allowed_users: set[int],
        poll_timeout: int = 30,
    ) -> None:
        self._service = service
        self._tx = transport
        self._allowed = allowed_users
        self._poll_timeout = poll_timeout

    async def run_forever(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        """Цикл long-polling; should_stop — точка останова (для тестов/сигналов)."""
        offset = 0
        while should_stop is None or not should_stop():
            updates = await self._tx.get_updates(offset, self._poll_timeout)
            for update in updates:
                offset = int(update["update_id"]) + 1
                await self.handle_update(update)

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            await self._handle_message(update["message"])
        elif "callback_query" in update:
            await self._handle_callback(update["callback_query"])

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat_id = int(message["chat"]["id"])
        user_id = int(message.get("from", {}).get("id", 0))
        text = str(message.get("text", "")).strip()
        if not self._authorized(user_id):
            await self._tx.send_message(chat_id, "⛔ Доступ запрещён.")
            return
        if not text:
            return
        if text.startswith("/"):
            await self._tx.send_message(
                chat_id, "Пришлите задачу текстом — я запущу агентный run и покажу ход."
            )
            return
        run_id = await self._service.create_run(text, None)
        await self._stream_to_chat(chat_id, run_id)

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback["id"])
        user_id = int(callback.get("from", {}).get("id", 0))
        message = callback.get("message", {})
        chat_id = int(message.get("chat", {}).get("id", 0))
        data = str(callback.get("data", ""))
        await self._tx.answer_callback(callback_id)
        if not self._authorized(user_id) or ":" not in data:
            return
        verb, approval_id = data.split(":", 1)
        approved = verb == "approve"
        run_id = await self._service.decide_approval(
            approval_id, approved=approved, reason=None if approved else "отклонено в Telegram"
        )
        await self._tx.send_message(
            chat_id, "✅ Одобрено, продолжаю." if approved else "🚫 Отклонено."
        )
        await self._service.resume_run(run_id)
        await self._stream_to_chat(chat_id, run_id)

    async def _stream_to_chat(self, chat_id: int, run_id: str) -> None:
        """Проиграть события run'а в чат до его завершения/приостановки."""
        tools: list[str] = []
        async for event in self._service.stream(run_id):
            kind = event.get("type")
            if kind == "tool_call":
                tools.append(str(event.get("tool")))
            elif kind == "notify":
                await self._tx.send_message(
                    chat_id, f"⚡ {event.get('tool')}: {event.get('reason')}"
                )
            elif kind == "run_finished":
                await self._report_finish(chat_id, run_id, event, tools)
                return

    async def _report_finish(
        self, chat_id: int, run_id: str, event: dict[str, Any], tools: list[str]
    ) -> None:
        state = event.get("state")
        used = f"\n\n🔧 {', '.join(tools)}" if tools else ""
        if state == "completed":
            answer = str(event.get("final_answer") or "(готово)")
            await self._tx.send_message(chat_id, _clip(answer + used))
        elif state == "waiting_approval":
            await self._send_approval_request(chat_id, run_id)
        else:
            error = event.get("error") or state
            await self._tx.send_message(chat_id, f"⚠️ Run {state}: {error}")

    async def _send_approval_request(self, chat_id: int, run_id: str) -> None:
        for approval in await self._service.list_pending_approvals():
            if approval.run_id != run_id:
                continue
            payload = approval.payload
            action = payload.get("tool") or approval.action_type
            args = payload.get("arguments")
            reason = payload.get("reason", "")
            # Approval показывает фактическое действие, не пересказ агента (§12).
            body = f"🔐 Требуется подтверждение\nДействие: {action}"
            if args:
                body += f"\nАргументы: {args}"
            if reason:
                body += f"\nПричина: {reason}"
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Одобрить", "callback_data": f"approve:{approval.approval_id}"},
                        {"text": "🚫 Отклонить", "callback_data": f"deny:{approval.approval_id}"},
                    ]
                ]
            }
            await self._tx.send_message(chat_id, _clip(body), reply_markup=keyboard)

    def _authorized(self, user_id: int) -> bool:
        return user_id in self._allowed


def _clip(text: str) -> str:
    if len(text) <= _MSG_LIMIT:
        return text
    return text[:_MSG_LIMIT] + "\n… [обрезано]"
