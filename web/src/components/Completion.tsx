import { useEffect, useRef } from "react";

import "./Completion.css";

export interface CompletionItem {
  value: string;
  label: string;
  description: string;
}

/** Тот же id связывает `role="listbox"` здесь с `aria-controls` у
    `<textarea role="combobox">` в Composer.tsx — константа в одном месте,
    чтобы строки не разошлись при правке одного файла без другого. */
export const COMPLETION_LISTBOX_ID = "composer-completion-listbox";

export function Completion({
  items,
  active,
  onPick,
  listboxId = COMPLETION_LISTBOX_ID,
}: {
  items: CompletionItem[];
  active: number;
  onPick: (value: string) => void;
  /** Свой id, когда на странице больше одного комбобокса (модальный пикер
      рендерится ПОВЕРХ композера — дубликат id ломал бы aria-controls). */
  listboxId?: string;
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
    <ul
      id={listboxId}
      className="completion"
      role="listbox"
      aria-label="Подсказки ввода"
    >
      {items.map((item, index) => (
        <li
          key={item.value}
          id={`${listboxId}-option-${index}`}
          ref={index === active ? activeRow : undefined}
          role="option"
          aria-selected={index === active}
          className={`completion__row${index === active ? " completion__row--active" : ""}`}
          // Без preventDefault на mousedown клик сначала уводит фокус с
          // textarea (mousedown — до click), и последующий
          // field.current?.setSelectionRange в pick() промахивается мимо
          // уже небрежно расфокусированного поля.
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => onPick(item.value)}
        >
          <span className="completion__label">{item.label}</span>
          <span className="completion__hint">{item.description}</span>
        </li>
      ))}
    </ul>
  );
}
