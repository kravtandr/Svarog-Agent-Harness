import { useEffect, useRef, useState } from "react";

/** Скорость печати: символ в 25 мс — заметно, но не тянет. */
const TICK_MS = 25;

/**
 * Название с анимацией набора текста (спека 2026-08-05).
 *
 * Первый маунт рендерит текст сразу: при загрузке списка чатов ничего
 * «печататься» не должно. Анимация — только на смену text (пуш нового
 * названия). prefers-reduced-motion отключает её совсем.
 */
export function AnimatedTitle({
  text,
  className,
}: {
  text: string;
  className?: string;
}) {
  const [shown, setShown] = useState(text);
  const prev = useRef(text);

  useEffect(() => {
    if (prev.current === text) return;
    prev.current = text;
    const reduce =
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    if (reduce) {
      setShown(text);
      return;
    }
    let i = 0;
    setShown("");
    const timer = window.setInterval(() => {
      i += 1;
      setShown(text.slice(0, i));
      if (i >= text.length) window.clearInterval(timer);
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, [text]);

  return <span className={className}>{shown}</span>;
}
