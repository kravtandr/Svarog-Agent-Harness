export interface ParsedServer {
  name: string;
  command: string;
  args: string[];
  envRefs: string[];
}

/** Токенизация shell-строки с уважением к кавычкам: пути с пробелами в
    аргументах MCP-серверов обычны, а split(/\s+/) их молча ломает.
    Экспортируется ради поля «Аргументы» на экране: разбирать его иначе, чем
    вставку, значило бы ломать при первой же правке то, что вставка сохранила. */
export function shellSplit(line: string): string[] {
  const out: string[] = [];
  let current = "";
  let quote: '"' | "'" | null = null;
  let started = false;
  for (const char of line) {
    if (quote !== null) {
      if (char === quote) quote = null;
      else current += char;
      continue;
    }
    if (char === '"' || char === "'") {
      quote = char;
      started = true;
      continue;
    }
    if (/\s/.test(char)) {
      if (started) out.push(current);
      current = "";
      started = false;
      continue;
    }
    current += char;
    started = true;
  }
  if (started) out.push(current);
  return out;
}

/** Обратная к shellSplit сборка для показа в поле ввода: аргумент с пробелом
    или кавычкой возвращается закавыченным, иначе следующий разбор того же
    поля расщепил бы путь вроде «/Users/a b/proj» на два аргумента.
    Экранирования обратным слэшем нет намеренно — его нет и в shellSplit,
    иначе вставленный windows-путь C:\Users\x потерял бы разделители; вместо
    него выбирается та кавычка, которой внутри аргумента нет. */
export function shellJoin(args: string[]): string {
  return args.map(quoteArg).join(" ");
}

function quoteArg(arg: string): string {
  if (arg === "") return '""';
  if (!/[\s"']/.test(arg)) return arg;
  if (!arg.includes('"')) return `"${arg}"`;
  if (!arg.includes("'")) return `'${arg}'`;
  // Внутри есть обе кавычки: аргумент собирается из соседних закавыченных
  // кусков — shellSplit склеивает их обратно, потому что закрытая кавычка
  // токен не заканчивает, это делает только пробел.
  return arg
    .split('"')
    .map((chunk) => `"${chunk}"`)
    .join("'\"'");
}

/** Правило имени сервера на бэкенде (`add_mcp`): латиница/цифры/дефис/
    подчёркивание, начинается с буквы. Кандидат, который ему не отвечает,
    предлагать бессмысленно — сохранение отвергнет его с ошибкой. */
const NAME_OK = /^[A-Za-z][\w-]{0,63}$/;

/** Имя сервера из аргументов: `@modelcontextprotocol/server-github` → github,
    `mcp-server-fetch` → fetch. Флаги (-y, --port) именем быть не могут — как и
    их значения: в `uvx --with mcp<2 mcp-server-fetch` кандидат `mcp<2` внешне
    похож на пакет, но именем быть не может, и раньше форма подставляла именно
    его. Перебираем дальше, пока не найдём годное. */
function guessName(command: string, args: string[]): string {
  for (const arg of args) {
    if (arg.startsWith("-")) continue;
    const tail = arg.split("/").pop() ?? arg;
    const stripped = tail.replace(/^(mcp-server-|server-|mcp-)/, "");
    if (NAME_OK.test(stripped)) return stripped;
  }
  return command;
}

function fromObject(raw: unknown, fallbackName: string): ParsedServer | null {
  if (typeof raw !== "object" || raw === null) return null;
  const entry = raw as Record<string, unknown>;
  const command = typeof entry.command === "string" ? entry.command.trim() : "";
  if (command === "") return null;
  const args = Array.isArray(entry.args)
    ? entry.args.filter((item): item is string => typeof item === "string")
    : [];
  // Только имена ключей env: значения из чужого конфига — живые токены, и
  // запись их в svarog.yaml была бы тихой утечкой (секреты живут в store).
  const envRefs =
    typeof entry.env === "object" && entry.env !== null
      ? Object.keys(entry.env as Record<string, unknown>)
      : [];
  const name = fallbackName !== "" ? fallbackName : guessName(command, args);
  return { name, command, args, envRefs };
}

export function parsePaste(text: string): ParsedServer | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;

  if (trimmed.startsWith("{")) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      return null;
    }
    const root = parsed as Record<string, unknown>;
    const servers = root.mcpServers;
    if (typeof servers === "object" && servers !== null) {
      const [first] = Object.entries(servers as Record<string, unknown>);
      if (first === undefined) return null;
      return fromObject(first[1], first[0]);
    }
    return fromObject(parsed, "");
  }

  const tokens = shellSplit(trimmed);
  const [command, ...args] = tokens;
  if (command === undefined || command === "") return null;
  return { name: guessName(command, args), command, args, envRefs: [] };
}
