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
 * Тикающая часть строки статуса (секундомер + опциональные токены/цена).
 * Живёт вне aria-live-региона: секундная перерисовка не должна вызывать
 * непрерывное озвучивание скринридером весь run (a11y-финдинг ревью).
 * Токены появляются после первой эмиссии с bridge (нулевой usage не
 * показываем — «0 токенов» читается как поломка), стоимость — когда
 * доросла до отображаемого цента.
 */
export function progressDetail(
  elapsedSec: number,
  progress: RunProgress | null,
): string {
  let detail = formatElapsed(elapsedSec);
  if (progress !== null && progress.tokens > 0) {
    detail += ` · ${formatTokens(progress.tokens)} токенов`;
    if (progress.costUsd >= 0.005)
      detail += ` · $${progress.costUsd.toFixed(2)}`;
  }
  return detail;
}

/** Строка статуса под лентой целиком (стабильный префикс + тикающий хвост). */
export function progressLabel(
  elapsedSec: number,
  progress: RunProgress | null,
  phase?: string | null,
): string {
  // Фаза заменяет общее «работает»: по нему нельзя отличить холодный старт
  // окружения от думающей минуту модели (трейс 06.08.2026).
  const head = phase
    ? phase.charAt(0).toUpperCase() + phase.slice(1)
    : "Сварог работает";
  return `${head}… ${progressDetail(elapsedSec, progress)}`;
}
