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
    paste: "uvx mcp-server-fetch",
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
    paste: "npx -y @modelcontextprotocol/server-postgres",
    risk: "high",
    hint: "Запросы к базе; нужен DATABASE_URL",
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
    paste: "uvx mcp-server-sqlite",
    risk: "medium",
    hint: "Запросы к локальному файлу базы",
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
    paste: "uvx mcp-server-time",
    risk: "low",
    hint: "Текущее время и часовые пояса",
  },
];
