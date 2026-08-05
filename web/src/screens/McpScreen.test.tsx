import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fakeApi } from "../test/fakeApi";
import { McpScreen } from "./McpScreen";

describe("вкладка MCP: подключение", () => {
  it("вставленная строка разбирается на поля", async () => {
    render(<McpScreen api={fakeApi()} />);
    await userEvent.type(
      screen.getByLabelText("Команда или JSON"),
      "uvx mcp-server-fetch",
    );
    await userEvent.click(screen.getByRole("button", { name: "Уточнить" }));
    expect(screen.getByLabelText("Имя")).toHaveValue("fetch");
    expect(screen.getByLabelText("Команда")).toHaveValue("uvx");
    expect(screen.getByLabelText("Аргументы")).toHaveValue("mcp-server-fetch");
  });

  it("клик по пресету заполняет поле вставки", async () => {
    render(<McpScreen api={fakeApi()} />);
    await userEvent.click(screen.getByRole("button", { name: /github/ }));
    expect(screen.getByLabelText("Команда или JSON")).toHaveValue(
      "npx -y @modelcontextprotocol/server-github",
    );
  });

  it("успешная проверка показывает инструменты чипами", async () => {
    const api = fakeApi({
      mcpTest: vi.fn().mockResolvedValue({
        ok: true,
        tools: ["fetch", "search"],
        error: null,
      }),
    });
    render(<McpScreen api={api} />);
    await userEvent.type(
      screen.getByLabelText("Команда или JSON"),
      "uvx mcp-server-fetch",
    );
    await userEvent.click(screen.getByRole("button", { name: "Проверить" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        /Сервер ответил — 2 инструмента/,
      ),
    );
    expect(screen.getByText("search")).toBeInTheDocument();
    expect(api.mcpTest).toHaveBeenCalledWith({
      command: "uvx",
      args: ["mcp-server-fetch"],
      env_refs: [],
    });
  });

  it("провал проверки показан честно", async () => {
    const api = fakeApi({
      mcpTest: vi.fn().mockResolvedValue({
        ok: false,
        tools: [],
        error: "команда не найдена",
      }),
    });
    render(<McpScreen api={api} />);
    await userEvent.type(
      screen.getByLabelText("Команда или JSON"),
      "нет-такой",
    );
    await userEvent.click(screen.getByRole("button", { name: "Проверить" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "команда не найдена",
      ),
    );
  });

  it("после провала проверки подключение требует второго клика", async () => {
    const api = fakeApi({
      mcpTest: vi
        .fn()
        .mockResolvedValue({ ok: false, tools: [], error: "нет бинаря" }),
    });
    render(<McpScreen api={api} />);
    await userEvent.type(
      screen.getByLabelText("Команда или JSON"),
      "нет-такой",
    );
    await userEvent.click(screen.getByRole("button", { name: "Проверить" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("нет бинаря"),
    );
    await userEvent.click(screen.getByRole("button", { name: "Подключить" }));
    expect(api.mcpAdd).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "Всё равно подключить?" }),
    );
    await waitFor(() => expect(api.mcpAdd).toHaveBeenCalled());
  });

  it("правка команды сбрасывает результат проверки", async () => {
    const api = fakeApi({
      mcpTest: vi
        .fn()
        .mockResolvedValue({ ok: true, tools: ["fetch"], error: null }),
    });
    render(<McpScreen api={api} />);
    const paste = screen.getByLabelText("Команда или JSON");
    await userEvent.type(paste, "uvx mcp-server-fetch");
    await userEvent.click(screen.getByRole("button", { name: "Проверить" }));
    await waitFor(() => expect(screen.getByRole("status")).toBeInTheDocument());
    await userEvent.type(paste, "-другое");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("значение секрета из вставленного JSON не уходит в сохранение", async () => {
    const api = fakeApi();
    render(<McpScreen api={api} />);
    await userEvent.click(screen.getByLabelText("Команда или JSON"));
    await userEvent.paste(
      JSON.stringify({
        mcpServers: {
          gh: {
            command: "npx",
            args: ["-y", "@modelcontextprotocol/server-github"],
            env: { GITHUB_TOKEN: "ghp_живой" },
          },
        },
      }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Подключить" }));
    await waitFor(() =>
      expect(api.mcpAdd).toHaveBeenCalledWith({
        name: "gh",
        command: "npx",
        args: ["-y", "@modelcontextprotocol/server-github"],
        env_refs: ["GITHUB_TOKEN"],
        risk: "high",
      }),
    );
    expect(JSON.stringify(vi.mocked(api.mcpAdd).mock.calls)).not.toContain(
      "ghp_живой",
    );
  });

  it("выбранный риск объясняет последствие", async () => {
    render(<McpScreen api={fakeApi()} />);
    await userEvent.click(screen.getByRole("radio", { name: "critical" }));
    expect(screen.getByText(/не отключается ни правилом/)).toBeInTheDocument();
  });
});
