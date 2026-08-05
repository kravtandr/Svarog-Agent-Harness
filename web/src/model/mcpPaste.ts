export interface ParsedServer {
  name: string;
  command: string;
  args: string[];
  envRefs: string[];
}

/** Токенизация shell-строки с уважением к кавычкам: пути с пробелами в
    аргументах MCP-серверов обычны, а split(/\s+/) их молча ломает. */
function tokenize(line: string): string[] {
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

/** Имя сервера из аргументов: `@modelcontextprotocol/server-github` → github,
    `mcp-server-fetch` → fetch. Флаги (-y, --port) именем быть не могут. */
function guessName(command: string, args: string[]): string {
  for (const arg of args) {
    if (arg.startsWith("-")) continue;
    const tail = arg.split("/").pop() ?? arg;
    const stripped = tail.replace(/^(mcp-server-|server-|mcp-)/, "");
    if (stripped !== "") return stripped;
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

  const tokens = tokenize(trimmed);
  const [command, ...args] = tokens;
  if (command === undefined || command === "") return null;
  return { name: guessName(command, args), command, args, envRefs: [] };
}
