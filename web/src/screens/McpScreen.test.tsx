import { act, render, screen, waitFor } from "@testing-library/react";
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

  it("повторная проверка снимает согласие «всё равно подключить»", async () => {
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
    expect(
      screen.getByRole("button", { name: "Всё равно подключить?" }),
    ).toBeInTheDocument();

    // Человек ничего не поправил, но перепроверяет — новый провал должен
    // получить своё собственное согласие, а не унаследовать старое.
    await userEvent.click(screen.getByRole("button", { name: "Проверить" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("нет бинаря"),
    );
    expect(
      screen.getByRole("button", { name: "Подключить" }),
    ).toBeInTheDocument();

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

describe("вкладка MCP: подключённые серверы", () => {
  const server = {
    name: "github",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-github"],
    env_refs: ["GITHUB_TOKEN"],
    risk: "high",
  };

  it("показывает карточку с командой, риском и секретами", async () => {
    const api = fakeApi({ mcpList: vi.fn().mockResolvedValue([server]) });
    render(<McpScreen api={api} />);
    await waitFor(() => expect(screen.getByText("github")).toBeInTheDocument());
    expect(
      screen.getByText("npx -y @modelcontextprotocol/server-github"),
    ).toBeInTheDocument();
    expect(screen.getByText("высокий риск")).toBeInTheDocument();
    expect(screen.getByText("GITHUB_TOKEN")).toBeInTheDocument();
  });

  it("«Инструменты» опрашивает именно этот сервер и рисует чипы", async () => {
    const api = fakeApi({
      mcpList: vi.fn().mockResolvedValue([server]),
      mcpTest: vi.fn().mockResolvedValue({
        ok: true,
        tools: ["create_issue", "search_code"],
        error: null,
      }),
    });
    render(<McpScreen api={api} />);
    await waitFor(() => expect(screen.getByText("github")).toBeInTheDocument());
    await userEvent.click(
      screen.getByRole("button", { name: "Инструменты github" }),
    );
    await waitFor(() =>
      expect(screen.getByText("create_issue")).toBeInTheDocument(),
    );
    expect(api.mcpTest).toHaveBeenCalledWith({
      command: "npx",
      args: ["-y", "@modelcontextprotocol/server-github"],
      env_refs: ["GITHUB_TOKEN"],
    });
  });

  it("мёртвый сервер показывает ошибку, а не пустой список", async () => {
    const api = fakeApi({
      mcpList: vi.fn().mockResolvedValue([server]),
      mcpTest: vi
        .fn()
        .mockResolvedValue({ ok: false, tools: [], error: "npx не найден" }),
    });
    render(<McpScreen api={api} />);
    await waitFor(() => expect(screen.getByText("github")).toBeInTheDocument());
    await userEvent.click(
      screen.getByRole("button", { name: "Инструменты github" }),
    );
    await waitFor(() =>
      expect(screen.getByText("npx не найден")).toBeInTheDocument(),
    );
  });

  it("не опрашивает серверы сама при открытии вкладки", async () => {
    const api = fakeApi({ mcpList: vi.fn().mockResolvedValue([server]) });
    render(<McpScreen api={api} />);
    await waitFor(() => expect(screen.getByText("github")).toBeInTheDocument());
    expect(api.mcpTest).not.toHaveBeenCalled();
  });

  it("удаляет сервер после повторного клика", async () => {
    const api = fakeApi({ mcpList: vi.fn().mockResolvedValue([server]) });
    render(<McpScreen api={api} />);
    await waitFor(() => expect(screen.getByText("github")).toBeInTheDocument());
    await userEvent.click(
      screen.getByRole("button", { name: "Удалить github" }),
    );
    expect(api.mcpRemove).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "Точно удалить?" }),
    );
    await waitFor(() => expect(api.mcpRemove).toHaveBeenCalledWith("github"));
  });

  it("взведённое согласие на удаление не переживает перезагрузку списка по другой причине", async () => {
    // Список возвращается дважды: mockResolvedValueOnce каждый раз создаёт
    // новый массив (как настоящий api.mcpList после реального запроса) —
    // если бы обе перезагрузки отдавали один и тот же массив, React счёл бы
    // состояние неизменным и не перезапустил бы эффект, который проверяем.
    const api = fakeApi({
      mcpList: vi
        .fn()
        .mockResolvedValueOnce([server])
        .mockResolvedValueOnce([server]),
    });
    render(<McpScreen api={api} />);
    await waitFor(() => expect(screen.getByText("github")).toBeInTheDocument());
    await userEvent.click(
      screen.getByRole("button", { name: "Удалить github" }),
    );
    expect(
      screen.getByRole("button", { name: "Точно удалить?" }),
    ).toBeInTheDocument();

    // Несвязанное действие — подключение совсем другого сервера — тоже
    // перезагружает список и должно снять согласие, взятое под github.
    await userEvent.type(
      screen.getByLabelText("Команда или JSON"),
      "uvx mcp-server-fetch",
    );
    await userEvent.click(screen.getByRole("button", { name: "Подключить" }));
    await waitFor(() => expect(api.mcpAdd).toHaveBeenCalled());

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Удалить github" }),
      ).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("button", { name: "Точно удалить?" }),
    ).not.toBeInTheDocument();
    expect(api.mcpRemove).not.toHaveBeenCalled();
  });

  it("гонка опроса: ответ для удалённой конфигурации не приземляется на пересозданную карточку", async () => {
    // В отличие от соседнего теста «Инструменты» опрашивает именно этот
    // сервер — здесь запрос не отвечает сразу: сервер под тем же именем
    // удаляют и пересоздают с другим составом args, пока проверка ещё летит.
    let resolveTest: (value: {
      ok: boolean;
      tools: string[];
      error: string | null;
    }) => void = () => {};
    const pending = new Promise<{
      ok: boolean;
      tools: string[];
      error: string | null;
    }>((resolve) => {
      resolveTest = resolve;
    });
    const recreated = {
      ...server,
      args: ["-y", "@modelcontextprotocol/server-github", "--readonly"],
    };
    const api = fakeApi({
      mcpList: vi
        .fn()
        .mockResolvedValueOnce([server])
        .mockResolvedValueOnce([recreated]),
      mcpTest: vi.fn().mockReturnValue(pending),
    });
    render(<McpScreen api={api} />);
    await waitFor(() => expect(screen.getByText("github")).toBeInTheDocument());

    // Запрос на проверку улетел для старой конфигурации сервера...
    await userEvent.click(
      screen.getByRole("button", { name: "Инструменты github" }),
    );

    // ...а пока он в полёте, сервер удаляют и пересоздают под тем же именем
    // с другим набором аргументов.
    await userEvent.click(
      screen.getByRole("button", { name: "Удалить github" }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Точно удалить?" }),
    );
    await waitFor(() =>
      expect(
        screen.getByText(
          "npx -y @modelcontextprotocol/server-github --readonly",
        ),
      ).toBeInTheDocument(),
    );

    // Старый запрос наконец отвечает — результат принадлежит уже не
    // существующей конфигурации и не должен появиться на новой карточке.
    await act(async () => {
      resolveTest({ ok: true, tools: ["create_issue"], error: null });
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    expect(screen.queryByText("create_issue")).not.toBeInTheDocument();
  });
});
