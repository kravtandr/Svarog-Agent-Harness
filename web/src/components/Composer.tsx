import { useState } from "react";

import {
  AUTONOMY_LABELS,
  EXECUTOR_LABELS,
  type Autonomy,
  type ExecutorKind,
  type ModelCard,
  type ProviderCard,
} from "../api/types";
import { ModelPicker } from "./ModelPicker";
import "./Composer.css";

export function Composer({
  onSend,
  autonomy,
  onAutonomyChange,
  executor,
  onExecutorChange,
  providers,
  provider,
  onProviderChange,
  model,
  models,
  modelsError,
  onModelChange,
}: {
  onSend: (text: string) => void;
  autonomy: Autonomy;
  onAutonomyChange: (autonomy: Autonomy) => void;
  executor: ExecutorKind | null;
  onExecutorChange: (executor: ExecutorKind) => void;
  providers: ProviderCard[];
  provider: string;
  onProviderChange: (name: string) => void;
  model: string;
  models: ModelCard[];
  modelsError: string | null;
  onModelChange: (id: string) => void;
}) {
  const [text, setText] = useState("");
  const [picking, setPicking] = useState(false);
  const external = executor === "external";

  function send() {
    const trimmed = text.trim();
    if (!trimmed) return;
    onSend(trimmed);
    setText("");
  }

  return (
    <div className="composer">
      <div className="composer__inner">
        {picking && (
          <div className="composer__picker">
            <ModelPicker
              models={models}
              current={model}
              error={modelsError}
              onPick={(id) => {
                onModelChange(id);
                setPicking(false);
              }}
              onClose={() => setPicking(false)}
            />
          </div>
        )}
        <div className="composer__box">
          <textarea
            className="composer__field"
            aria-label="Написать Сварогу"
            placeholder="Написать Сварогу"
            rows={1}
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              // Enter отправляет, Shift+Enter — перенос строки: так ведёт
              // себя любой чат, и без этого поле кажется сломанным.
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                send();
              }
            }}
          />
          <div className="composer__foot">
            {/* Автономия — свойство сообщения, её принимает POST /sessions/{id}/messages.
                Исполнитель, провайдер и модель — тоже свойство сообщения (override):
                конфиг остаётся значением по умолчанию, а не единственным вариантом. */}
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
              <select
                className="composer__select"
                aria-label="Исполнитель"
                value={executor ?? ""}
                // Пока /config не ответил, не знаем реальный executor.type —
                // список закрыт для выбора: угаданное значение хуже пустого.
                disabled={executor === null}
                onChange={(event) =>
                  onExecutorChange(event.target.value as ExecutorKind)
                }
              >
                {executor === null && (
                  <option value="" disabled>
                    исполнитель…
                  </option>
                )}
                {(Object.keys(EXECUTOR_LABELS) as ExecutorKind[]).map(
                  (kind) => (
                    <option key={kind} value={kind}>
                      {EXECUTOR_LABELS[kind]}
                    </option>
                  ),
                )}
              </select>
              <span className="composer__dot" aria-hidden="true">
                ·
              </span>
              {providers.length > 1 && (
                <select
                  className="composer__select"
                  aria-label="Провайдер"
                  value={provider}
                  disabled={external}
                  onChange={(event) => onProviderChange(event.target.value)}
                >
                  {providers.map((card) => (
                    <option key={card.name} value={card.name}>
                      {card.name}
                    </option>
                  ))}
                </select>
              )}
              <button
                type="button"
                className="composer__model"
                aria-label="Выбрать модель"
                disabled={external}
                // Внешний агент ходит к своему провайдеру
                // (executor.external.base_url) — модель отсюда на него не влияет.
                title={
                  external
                    ? "Внешний агент ходит к своему провайдеру"
                    : "Выбрать модель"
                }
                onClick={() => setPicking(true)}
              >
                {model === "" ? "модель…" : model}
              </button>
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
