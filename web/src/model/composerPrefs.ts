/** Последний выбор контролов композера — в localStorage браузера.
 *
 * Сервер тут не участвует сознательно: это предпочтение конкретного
 * браузера, а не конфиг воркспейса (svarog.yaml остаётся источником
 * дефолтов). Сохранённое значение применяется только если оно всё ещё
 * существует в списках с сервера — исчезнувший провайдер или исполнитель
 * тихо откатывается на дефолт конфига.
 */

export interface ComposerPrefs {
  autonomy?: string;
  executor?: string;
  sandbox?: string;
  provider?: string;
  model?: string;
}

const KEY = "svarog.composer";

/** Битый JSON или отключённый localStorage (private mode) — просто пусто. */
export function loadPrefs(): ComposerPrefs {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (raw === null) return {};
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object") return {};
    return parsed as ComposerPrefs;
  } catch {
    return {};
  }
}

export function savePref(patch: Partial<ComposerPrefs>): void {
  try {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({ ...loadPrefs(), ...patch }),
    );
  } catch {
    // Запись недоступна (квота, private mode) — выбор просто не запомнится.
  }
}
