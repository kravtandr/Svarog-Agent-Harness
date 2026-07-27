import { useCallback, useEffect, useRef, useState } from "react";

import type { Api } from "../api/client";
import { subscribeRun } from "../api/stream";
import type { Autonomy } from "../api/types";
import { Composer } from "../components/Composer";
import { Gate } from "../components/Gate";
import { ToolCalls } from "../components/ToolCalls";
import { applyEvent, fromHistory, type ThreadItem } from "../model/thread";
import "./ChatScreen.css";

type Call = Extract<ThreadItem, { kind: "call" }>;
type Entry = ThreadItem | { kind: "calls"; id: string; calls: Call[] };

/** Подряд идущие вызовы рисуются одной группой, а не по карточке на каждый. */
function groupItems(items: ThreadItem[]): Entry[] {
  const grouped: Entry[] = [];
  for (const item of items) {
    const last = grouped[grouped.length - 1];
    if (item.kind === "call") {
      if (last !== undefined && last.kind === "calls") {
        last.calls.push(item);
        continue;
      }
      grouped.push({ kind: "calls", id: `g-${item.id}`, calls: [item] });
      continue;
    }
    grouped.push(item);
  }
  return grouped;
}

export function ChatScreen({
  api,
  sessionId,
  loading = false,
  error = null,
  baseUrl = "",
  token,
}: {
  api: Api;
  sessionId: string | null;
  loading?: boolean;
  error?: string | null;
  baseUrl?: string;
  token?: string;
}) {
  const [items, setItems] = useState<ThreadItem[]>([]);
  const [threadError, setThreadError] = useState<string | null>(null);
  const [autonomy, setAutonomy] = useState<Autonomy>("supervised");
  const unsubscribe = useRef<(() => void) | null>(null);

  useEffect(() => {
    if (sessionId === null) return;
    api
      .sessionThread(sessionId)
      .then((thread) => setItems(fromHistory(thread.items)))
      .catch(() => setThreadError("Не удалось загрузить историю этой сессии."));
  }, [api, sessionId]);

  useEffect(() => () => unsubscribe.current?.(), []);

  const send = useCallback(
    async (text: string) => {
      if (sessionId === null) return;
      setItems((current) => [
        ...current,
        { kind: "user", id: `u-${current.length}`, text },
      ]);
      const ref = await api.sendMessage(sessionId, text, autonomy);
      unsubscribe.current?.();
      unsubscribe.current = subscribeRun(baseUrl, ref.run_id, token, (event) =>
        setItems((current) => applyEvent(current, event)),
      );
    },
    [api, sessionId, autonomy, baseUrl, token],
  );

  const decide = useCallback(
    async (approvalId: string, approved: boolean) => {
      await api.decideApproval(approvalId, approved);
      setItems((current) =>
        current.filter(
          (item) => !(item.kind === "gate" && item.approvalId === approvalId),
        ),
      );
    },
    [api],
  );

  const shown = error ?? threadError;

  return (
    <div className="chat">
      <div className="chat__thread">
        <div className="chat__col">
          {shown !== null && <p className="chat__error">{shown}</p>}
          {shown === null && loading && (
            <p className="chat__hint">Загружаем сессии…</p>
          )}
          {/* Пустой экран — приглашение к действию, а не «нет данных». */}
          {shown === null && !loading && items.length === 0 && (
            <p className="chat__hint">
              Поставьте задачу — Сварог заведёт ветку и покажет каждый свой шаг.
            </p>
          )}
          {groupItems(items).map((entry) => {
            if (entry.kind === "calls")
              return <ToolCalls key={entry.id} calls={entry.calls} />;
            if (entry.kind === "user")
              return (
                <div key={entry.id} className="chat__you">
                  {entry.text}
                </div>
              );
            if (entry.kind === "say")
              return (
                <div key={entry.id} className="chat__say">
                  {entry.text}
                </div>
              );
            if (entry.kind === "gate")
              return (
                <Gate
                  key={entry.id}
                  gate={entry}
                  onDecide={(approved) =>
                    void decide(entry.approvalId, approved)
                  }
                />
              );
            return null;
          })}
        </div>
      </div>
      <Composer
        onSend={(text) => void send(text)}
        autonomy={autonomy}
        onAutonomyChange={setAutonomy}
        executor="нативный цикл"
        model="qwen3-coder"
      />
    </div>
  );
}
