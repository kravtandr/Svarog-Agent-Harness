import { useState } from "react";

import { AUTONOMY_LABELS, type Autonomy } from "../api/types";
import "./Composer.css";

export function Composer({
  onSend,
  autonomy,
  onAutonomyChange,
  executor,
  model,
}: {
  onSend: (text: string) => void;
  autonomy: Autonomy;
  onAutonomyChange: (autonomy: Autonomy) => void;
  executor: string;
  model: string;
}) {
  const [text, setText] = useState("");

  function send() {
    const trimmed = text.trim();
    if (!trimmed) return;
    onSend(trimmed);
    setText("");
  }

  return (
    <div className="composer">
      <div className="composer__inner">
        <div className="composer__box">
          <textarea
            className="composer__field"
            aria-label="Написать Сварогу"
            placeholder="Написать Сварогу"
            rows={1}
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
          <div className="composer__foot">
            {/* Автономия — свойство сообщения, её принимает POST /sessions/{id}/messages.
                Исполнитель и модель живут в svarog.yaml и меняются в настройках. */}
            <span className="composer__modes">
              <select
                className="composer__select"
                aria-label="Автономия"
                value={autonomy}
                onChange={(event) =>
                  onAutonomyChange(event.target.value as Autonomy)
                }
              >
                {(Object.keys(AUTONOMY_LABELS) as Autonomy[]).map((mode) => (
                  <option key={mode} value={mode}>
                    {AUTONOMY_LABELS[mode]}
                  </option>
                ))}
              </select>
              <span className="composer__dot" aria-hidden="true">
                ·
              </span>
              <span className="composer__fixed" title="Меняется в настройках">
                {executor}
              </span>
              <span className="composer__dot" aria-hidden="true">
                ·
              </span>
              <span className="composer__fixed" title="Меняется в настройках">
                {model}
              </span>
            </span>
            <span className="composer__spacer" />
            {/* Место под голос занято сразу: включение не потребует переверстки. */}
            <button
              type="button"
              className="composer__icon"
              aria-label="Голосовой ввод"
              aria-describedby="mic-hint"
              disabled
            >
              ●
            </button>
            <span id="mic-hint" className="composer__hidden">
              Голосовой ввод появится позже
            </span>
            <button
              type="button"
              className="composer__icon composer__icon--send"
              aria-label="Отправить"
              onClick={send}
            >
              ↑
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
