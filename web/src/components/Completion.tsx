import { useEffect, useRef } from "react";

import "./Completion.css";

export interface CompletionItem {
  value: string;
  label: string;
  description: string;
}

export function Completion({
  items,
  active,
  onPick,
}: {
  items: CompletionItem[];
  active: number;
  onPick: (value: string) => void;
}) {
  const activeRow = useRef<HTMLLIElement>(null);

  // Список может выйти за видимую высоту (max-height на 8 строк); держим
  // активную строку в кадре при перелистывании клавишами. Индекс приходит
  // снаружи (композер решает, какая строка активна) — здесь только сам факт
  // прокрутки к уже известной активной строке, локальный для этого списка.
  useEffect(() => {
    // jsdom (тесты) не реализует scrollIntoView — проверяем на всякий случай.
    activeRow.current?.scrollIntoView?.({ block: "nearest" });
  }, [active]);

  // Пустой список не рисуем вовсе: рамка без строк читается как поломка.
  if (items.length === 0) return null;

  return (
    <ul className="completion" role="listbox" aria-label="Подсказки ввода">
      {items.map((item, index) => (
        <li
          key={item.value}
          id={`completion-option-${index}`}
          ref={index === active ? activeRow : undefined}
          role="option"
          aria-selected={index === active}
          className={`completion__row${index === active ? " completion__row--active" : ""}`}
          onClick={() => onPick(item.value)}
        >
          <span className="completion__label">{item.label}</span>
          <span className="completion__hint">{item.description}</span>
        </li>
      ))}
    </ul>
  );
}
