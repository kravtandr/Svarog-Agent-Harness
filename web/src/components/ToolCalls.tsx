import { useState } from "react";

import type { ThreadItem } from "../model/thread";
import "./ToolCalls.css";

type Call = Extract<ThreadItem, { kind: "call" }>;

const MARK: Record<Call["status"], string> = { ok: "✓", run: "▸", error: "✕" };

function CallRow({ call }: { call: Call }) {
  // Упавший вызов раскрыт изначально: прятать причину остановки бессмысленно.
  const [open, setOpen] = useState(call.status === "error");

  return (
    <>
      <button
        type="button"
        className={`call call--${call.status}`}
        aria-expanded={open}
        onClick={() => setOpen((was) => !was)}
      >
        <span className="call__mark" aria-hidden="true">
          {MARK[call.status]}
        </span>
        {call.server !== null && (
          <>
            <span className="call__server">{call.server}</span>
            <span className="call__slash" aria-hidden="true">
              /
            </span>
          </>
        )}
        <span className="call__name">{call.name}</span>
        <span className="call__arg">{call.arg}</span>
        <span className="call__result">{call.result}</span>
      </button>
      {open && (
        <div className="call-detail" data-testid="call-detail">
          <p className="call-detail__label">запрос</p>
          <pre>{call.arg || "—"}</pre>
          <p className="call-detail__label">ответ</p>
          <pre>{call.result || "—"}</pre>
        </div>
      )}
    </>
  );
}

export function ToolCalls({ calls }: { calls: Call[] }) {
  if (calls.length === 0) return null;
  return (
    <div className="calls">
      {calls.map((call) => (
        <CallRow key={call.id} call={call} />
      ))}
    </div>
  );
}
