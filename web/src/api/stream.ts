import type { StreamEvent } from "../model/thread";

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
