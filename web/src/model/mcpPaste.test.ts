import { describe, expect, it } from "vitest";

import { parsePaste } from "./mcpPaste";

describe("разбор вставленного MCP-конфига", () => {
  it("режет shell-строку на команду и аргументы", () => {
    expect(parsePaste("uvx mcp-server-fetch")).toEqual({
      name: "fetch",
      command: "uvx",
      args: ["mcp-server-fetch"],
      envRefs: [],
    });
  });

  it("уважает кавычки в аргументах", () => {
    expect(parsePaste('npx server --root "/Мои файлы/проект"')?.args).toEqual([
      "server",
      "--root",
      "/Мои файлы/проект",
    ]);
  });

  it("выводит имя из scoped-пакета", () => {
    expect(parsePaste("npx -y @modelcontextprotocol/server-github")?.name).toBe(
      "github",
    );
  });

  it("читает блок mcpServers из конфига Claude Desktop", () => {
    const text = JSON.stringify({
      mcpServers: {
        github: {
          command: "npx",
          args: ["-y", "@modelcontextprotocol/server-github"],
          env: { GITHUB_TOKEN: "ghp_живой_токен" },
        },
      },
    });
    expect(parsePaste(text)).toEqual({
      name: "github",
      command: "npx",
      args: ["-y", "@modelcontextprotocol/server-github"],
      envRefs: ["GITHUB_TOKEN"],
    });
  });

  it("значение секрета из вставки не сохраняется нигде", () => {
    const text = JSON.stringify({
      mcpServers: { gh: { command: "npx", env: { TOKEN: "ghp_секрет" } } },
    });
    expect(JSON.stringify(parsePaste(text))).not.toContain("ghp_секрет");
  });

  it("читает одиночный объект сервера", () => {
    const text = '{"command": "uvx", "args": ["mcp-server-time"]}';
    expect(parsePaste(text)).toEqual({
      name: "time",
      command: "uvx",
      args: ["mcp-server-time"],
      envRefs: [],
    });
  });

  it("на пустом и мусорном вводе возвращает null, а не полупустой объект", () => {
    expect(parsePaste("   ")).toBeNull();
    expect(parsePaste("{ поломанный json")).toBeNull();
    expect(parsePaste('{"args": ["без-команды"]}')).toBeNull();
  });

  it("если имя вывести не из чего — берёт команду", () => {
    expect(parsePaste("my-server")?.name).toBe("my-server");
  });
});
