import { describe, expect, it } from "vitest";

import { parsePaste, shellJoin, shellSplit } from "./mcpPaste";

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

describe("shellJoin: сборка аргументов для показа в поле", () => {
  it("аргумент с пробелом переживает круг сборка → разбор", () => {
    const args = ["-y", "server-filesystem", "/Users/a b/proj"];
    expect(shellSplit(shellJoin(args))).toEqual(args);
  });

  it("не трогает аргументы, которым кавычки не нужны", () => {
    expect(shellJoin(["-y", "@scope/pkg"])).toBe("-y @scope/pkg");
  });

  it("аргумент с кавычкой внутри закавычивается другой кавычкой", () => {
    // Экранирования обратным слэшем нет ни здесь, ни в shellSplit — круг
    // должен сойтись без него.
    expect(shellSplit(shellJoin(['it\'s "тут"']))).toEqual(['it\'s "тут"']);
    expect(shellSplit(shellJoin(["путь/с 'кавычкой'"]))).toEqual([
      "путь/с 'кавычкой'",
    ]);
  });

  it("пустой аргумент не исчезает при круге", () => {
    expect(shellSplit(shellJoin(["--flag", "", "x"]))).toEqual([
      "--flag",
      "",
      "x",
    ]);
  });
});
