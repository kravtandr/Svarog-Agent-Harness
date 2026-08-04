/** Живой прогресс run'а из WS-события `progress` (см. gateway/service.py). */
export type RunProgress = { tokens: number; costUsd: number };

/** 83 → "1:23" — секундомер в строке статуса. */
export function formatElapsed(totalSec: number): string {
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return `${min}:${String(sec).padStart(2, "0")}`;
}

/** 12400 → "12 400" — вручную, а не toLocaleString: у локалей неразрывные
 * пробелы разных видов, а строка сравнивается в тестах. */
function formatTokens(tokens: number): string {
  return String(tokens).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

/**
 * Строка статуса под лентой. Токены появляются после первой эмиссии с
 * bridge (нулевой usage не показываем — «0 токенов» читается как поломка),
 * стоимость — когда доросла до отображаемого цента.
 */
export function progressLabel(
  elapsedSec: number,
  progress: RunProgress | null,
): string {
  let label = `Сварог работает… ${formatElapsed(elapsedSec)}`;
  if (progress !== null && progress.tokens > 0) {
    label += ` · ${formatTokens(progress.tokens)} токенов`;
    if (progress.costUsd >= 0.005)
      label += ` · $${progress.costUsd.toFixed(2)}`;
  }
  return label;
}
