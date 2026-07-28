/**
 * Режим подсказок ввода — порт `cli/chat_completion.py: detect_completion`.
 *
 * Логика повторена в браузере, а не вынесена на сервер, потому что
 * определять режим надо на каждое нажатие клавиши: запрос по сети на
 * букву сделал бы поле ввода заметно медленнее набора.
 */
export type CompletionMode = "idle" | "slash" | "at";

export interface CompletionQuery {
  mode: CompletionMode;
  token: string;
}

export function detectCompletion(textBeforeCursor: string): CompletionQuery {
  const text = textBeforeCursor;
  if (text === "") return { mode: "idle", token: "" };

  // Токен от последнего пробела: приоритет у @, его можно писать после текста.
  let index = text.length - 1;
  while (index >= 0 && !" \t\n".includes(text[index])) index -= 1;
  const token = text.slice(index + 1);
  if (token.startsWith("@")) return { mode: "at", token };

  // Слэш-команда — только если строка начинается с / и курсор в первом токене.
  // Примечание: цикл поиска токена выше (строки 20–21) использует узкий набор " \t\n",
  // а здесь мы используем \s+ для удаления начальных пробелов (совпадает с Python lstrip).
  // Асимметрия намеренна: приём @ работает только со стандартными пробелами.
  const stripped = text.replace(/^\s+/, "");
  if (
    stripped.startsWith("/") &&
    !text.includes("\n") &&
    !stripped.includes(" ")
  ) {
    return { mode: "slash", token: stripped };
  }
  return { mode: "idle", token: "" };
}

export interface ParsedCommand {
  /** Пусто — команда не опознана; runCommand не отправляет её агенту. */
  name: string;
  args: string;
}

/**
 * Шесть команд, которые веб-чат исполняет сам — тот же список, что отдаёт
 * `GET /commands` (`gateway/commands.py: WEB_COMMANDS`), но зашит здесь же:
 * решение «это команда или обычное сообщение» нужно сразу при отправке, а не
 * после похода на сервер.
 */
const KNOWN_COMMANDS = new Set([
  "help",
  "new",
  "sessions",
  "executor",
  "policies",
  "copy",
]);

/**
 * `null` — не команда, отправлять как обычное сообщение.
 *
 * Слэш-режим определяет `detectCompletion` (тот же, что у автодополнения):
 * это нарочно исключает любой текст с пробелом или переносом строки после
 * первого слова — иначе реальное сообщение вида "/build the new feature"
 * никогда нельзя было бы отправить, потому что ведущий слэш блокировал бы
 * его как «команду» на каждую отправку.
 */
export function parseCommand(text: string): ParsedCommand | null {
  const query = detectCompletion(text);
  if (query.mode !== "slash") return null;
  const name = query.token.slice(1);
  if (KNOWN_COMMANDS.has(name)) return { name, args: "" };
  return { name: "", args: query.token };
}

export function replaceToken(
  text: string,
  caret: number,
  value: string,
): { text: string; caret: number } {
  const before = text.slice(0, caret);
  const after = text.slice(caret);
  let index = before.length - 1;
  while (index >= 0 && !" \t\n".includes(before[index])) index -= 1;
  const head = before.slice(0, index + 1);
  // Пробел после вставки: следующее слово не слипнется с путём, и
  // detectCompletion сразу вернётся в idle — меню закроется само.
  const next = `${head}${value} `;
  return { text: `${next}${after}`, caret: next.length };
}
