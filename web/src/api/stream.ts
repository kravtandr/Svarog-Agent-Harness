import type { StreamEvent } from "../model/thread";
import type { SessionEvent } from "./types";

/**
 * Подписка на события run'а. Возвращает функцию отписки.
 *
 * Токен передаётся query-параметром: WebSocket в браузере не позволяет
 * задать заголовок Authorization, и gateway это уже учитывает
 * (`websocket.query_params.get("token")`).
 */
export function subscribeRun(
  baseUrl: string,
  runId: string,
  token: string | undefined,
  onEvent: (event: StreamEvent) => void,
): () => void {
  const base = baseUrl || window.location.origin;
  const url = new URL(`/runs/${encodeURIComponent(runId)}/events`, base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  if (token) url.searchParams.set("token", token);

  const socket = new WebSocket(url);
  socket.onmessage = (message: MessageEvent<string>) => {
    try {
      onEvent(JSON.parse(message.data) as StreamEvent);
    } catch {
      // Битое событие пропускаем: одна плохая строка не должна валить ленту.
    }
  };
  return () => socket.close();
}

/**
 * Подписка на события сессий (названия чатов, спека 2026-08-05).
 *
 * Возвращает функцию отписки; onClose зовётся при закрытии сокета извне
 * (обрыв сети, рестарт сервера) — на нём клиент строит реконнект. Ручная
 * отписка onClose не зовёт.
 */
export function subscribeSessionEvents(
  baseUrl: string,
  token: string | undefined,
  onEvent: (event: SessionEvent) => void,
  onClose?: () => void,
): () => void {
  const base = baseUrl || window.location.origin;
  const url = new URL("/sessions/events", base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  if (token) url.searchParams.set("token", token);

  const socket = new WebSocket(url);
  socket.onmessage = (message: MessageEvent<string>) => {
    try {
      onEvent(JSON.parse(message.data) as SessionEvent);
    } catch {
      // Битое событие пропускаем: одна плохая строка не валит канал.
    }
  };
  if (onClose) socket.onclose = onClose;
  return () => {
    socket.onclose = null;
    socket.close();
  };
}
