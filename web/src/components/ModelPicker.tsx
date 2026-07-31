import { useEffect, useRef, useState } from "react";

import type { ModelCard, ProviderCard } from "../api/types";
import "./ModelPicker.css";

/** «163840» → «164K»: в строке списка важен порядок, а не точность. */
function context(length: number | null): string {
  if (length === null) return "";
  return length >= 1000 ? `${Math.round(length / 1000)}K` : String(length);
}

function price(card: ModelCard): string {
  if (card.input_usd_per_mtok === null) return "";
  return `$${card.input_usd_per_mtok}/${card.output_usd_per_mtok ?? "?"} за Mtok`;
}

export function ModelPicker({
  models,
  current,
  error,
  onPick,
  onClose,
  providers = [],
  provider = "",
  onProviderChange = () => {},
}: {
  models: ModelCard[];
  current: string;
  error: string | null;
  onPick: (id: string) => void;
  onClose: () => void;
  /** Провайдеры для переключения прямо в окошке: смена провайдера
      перезагружает список моделей (модели живут у провайдера). */
  providers?: ProviderCard[];
  provider?: string;
  onProviderChange?: (name: string) => void;
}) {
  const [query, setQuery] = useState("");
  const field = useRef<HTMLInputElement>(null);

  // Поиск в фокусе сразу: у OpenRouter моделей несколько сотен, и первое
  // действие человека здесь — печатать, а не листать.
  useEffect(() => field.current?.focus(), []);

  // На document, а не на onKeyDown внутри панели: фокус может уйти за её
  // пределы табом (например, на соседний элемент композера), и Escape
  // обязан закрывать панель независимо от того, где сейчас фокус.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const needle = query.trim().toLowerCase();
  const shown = models.filter(
    (card) =>
      needle === "" ||
      card.id.toLowerCase().includes(needle) ||
      (card.name ?? "").toLowerCase().includes(needle),
  );

  return (
    <div className="picker" role="dialog" aria-label="Выбор модели">
      {providers.length > 1 && (
        <select
          className="picker__provider"
          aria-label="Провайдер моделей"
          value={provider}
          onChange={(event) => onProviderChange(event.target.value)}
        >
          {providers.map((card) => (
            <option key={card.name} value={card.name}>
              {card.name}
            </option>
          ))}
        </select>
      )}
      <input
        ref={field}
        className="picker__search"
        aria-label="Поиск модели"
        placeholder="Поиск модели"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      {error !== null && <p className="picker__error">{error}</p>}
      {error === null && shown.length === 0 && (
        <p className="picker__empty">Ничего не нашлось.</p>
      )}
      <ul className="picker__list">
        {shown.map((card) => (
          <li key={card.id}>
            <button
              type="button"
              className={`picker__row${card.id === current ? " picker__row--current" : ""}`}
              onClick={() => onPick(card.id)}
            >
              <span className="picker__name">{card.name ?? card.id}</span>
              <span className="picker__meta">
                {context(card.context_length)} {price(card)}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
