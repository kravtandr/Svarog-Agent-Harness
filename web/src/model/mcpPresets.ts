import type { RiskLevel } from "./risk";

export interface McpPreset {
  id: string;
  title: string;
  /** Строка ровно в том виде, в каком человек вставил бы её сам, — попадает
      в поле вставки и проходит через parsePaste, а не в обход него. */
  paste: string;
  risk: RiskLevel;
  hint: string;
}

/** Фронтовая константа, а не эндпоинт: список меняется вместе с UI,
    версионируется вместе с ним и не требует ни сети, ни миграции конфига. */
export const MCP_PRESETS: McpPreset[] = [
  {
    id: "fetch",
    title: "fetch",
    // `mcp<2` — не наша прихоть: mcp-server-fetch объявляет `mcp>=1.1.3` без
    // верхней границы, а mcp 2.0 переименовал McpError, и пакет ломается о
    // собственную зависимость. Без пина команда не поднимается ни у кого.
    paste: "uvx --with mcp<2 mcp-server-fetch",
    risk: "medium",
    hint: "Загружает страницы по URL",
  },
  {
    id: "filesystem",
    title: "filesystem",
    paste: "npx -y @modelcontextprotocol/server-filesystem .",
    risk: "high",
    hint: "Читает и пишет файлы в указанном каталоге",
  },
  {
    id: "github",
    title: "github",
    paste: "npx -y @modelcontextprotocol/server-github",
    risk: "high",
    hint: "Issues, PR и поиск по коду; нужен GITHUB_TOKEN",
  },
  {
    id: "postgres",
    title: "postgres",
    // URL идёт аргументом, а не через env: без него сервер печатает
    // «Please provide a database URL» и закрывается. В строке — образец,
    // который человек правит под свою базу.
    paste:
      "npx -y @modelcontextprotocol/server-postgres postgresql://localhost/mydb",
    risk: "high",
    hint: "Запросы к базе; впишите свой URL в команду",
  },
  {
    id: "playwright",
    title: "playwright",
    paste: "npx -y @playwright/mcp",
    risk: "high",
    hint: "Управляет браузером",
  },
  {
    id: "sqlite",
    title: "sqlite",
    // Пин по той же причине, что у fetch: в mcp 2.0 у Server не осталось
    // list_resources. Путь к базе обязателен — сервер без него не стартует.
    paste: "uvx --with mcp<2 mcp-server-sqlite --db-path ~/db.sqlite",
    risk: "medium",
    hint: "Запросы к локальному файлу базы; укажите свой путь",
  },
  {
    id: "memory",
    title: "memory",
    paste: "npx -y @modelcontextprotocol/server-memory",
    risk: "low",
    hint: "Граф знаний в памяти процесса",
  },
  {
    id: "time",
    title: "time",
    // Пин по той же причине, что у fetch.
    paste: "uvx --with mcp<2 mcp-server-time",
    risk: "low",
    hint: "Текущее время и часовые пояса",
  },
];
