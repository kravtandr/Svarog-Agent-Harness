import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fakeApi } from "../test/fakeApi";
import { McpScreen } from "./McpScreen";

describe("вкладка MCP", () => {
  it("показывает подключённые серверы", async () => {
    const api = fakeApi({
      mcpList: vi.fn().mockResolvedValue([
        {
          name: "fetch",
          command: "uvx",
          args: ["mcp-server-fetch"],
          env_refs: [],
          risk: "medium",
        },
      ]),
    });
    render(<McpScreen api={api} />);
    await waitFor(() => expect(screen.getByText("fetch")).toBeInTheDocument());
    expect(screen.getByText(/uvx mcp-server-fetch/)).toBeInTheDocument();
  });

  it("проверка подключения показывает инструменты при успехе", async () => {
    const api = fakeApi({
      mcpTest: vi.fn().mockResolvedValue({
        ok: true,
        tools: ["fetch", "search"],
        error: null,
      }),
    });
    render(<McpScreen api={api} />);
    await userEvent.type(screen.getByLabelText("Команда (stdio)"), "uvx");
    await userEvent.click(
      screen.getByRole("button", { name: "Проверить подключение" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("fetch, search"),
    );
    expect(api.mcpTest).toHaveBeenCalledWith({
      command: "uvx",
      args: [],
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
    await userEvent.type(screen.getByLabelText("Команда (stdio)"), "нет-такой");
    await userEvent.click(
      screen.getByRole("button", { name: "Проверить подключение" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "команда не найдена",
      ),
    );
  });

  it("сохранение уходит с распарсенными аргументами и env_refs", async () => {
    const api = fakeApi();
    render(<McpScreen api={api} />);
    await userEvent.type(screen.getByLabelText("Имя"), "gh");
    await userEvent.type(screen.getByLabelText("Команда (stdio)"), "npx");
    await userEvent.type(
      screen.getByLabelText("Аргументы (через пробел)"),
      "-y @modelcontextprotocol/server-github",
    );
    await userEvent.type(
      screen.getByLabelText("Секреты для env (имена через запятую)"),
      "GITHUB_TOKEN",
    );
    await userEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() =>
      expect(api.mcpAdd).toHaveBeenCalledWith({
        name: "gh",
        command: "npx",
        args: ["-y", "@modelcontextprotocol/server-github"],
        env_refs: ["GITHUB_TOKEN"],
        risk: "high",
      }),
    );
  });
});
