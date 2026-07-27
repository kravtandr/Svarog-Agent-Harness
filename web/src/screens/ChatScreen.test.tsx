import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Api } from "../api/client";
import { fakeApi } from "../test/fakeApi";
import { ChatScreen } from "./ChatScreen";

const thread = {
  session_id: "s1",
  title: "FTS-поиск по памяти",
  items: [
    {
      kind: "user" as const,
      text: "Добавь FTS-поиск",
      server: null,
      name: "",
      arg: "",
      result: "",
      status: "",
    },
    {
      kind: "call" as const,
      text: "",
      server: null,
      name: "write_file",
      arg: "memory/index.py",
      result: "записано 1234 символов",
      status: "succeeded",
    },
  ],
};

const api = (over: Partial<Api> = {}): Api =>
  fakeApi({ sessionThread: () => Promise.resolve(thread), ...over });

describe("экран диалога", () => {
  it("рисует историю выбранной сессии", async () => {
    render(
      <ChatScreen
        api={api()}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );

    await waitFor(() =>
      expect(screen.getByText("Добавь FTS-поиск")).toBeInTheDocument(),
    );
    expect(screen.getByText("write_file")).toBeInTheDocument();
    expect(screen.getByText("записано 1234 символов")).toBeInTheDocument();
  });

  it("отправляет сообщение в текущую сессию", async () => {
    const client = api();
    render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );
    await waitFor(() =>
      expect(screen.getByText("Добавь FTS-поиск")).toBeInTheDocument(),
    );

    await userEvent.type(
      screen.getByRole("textbox", { name: /написать/i }),
      "прогони тесты",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    expect(client.sendMessage).toHaveBeenCalledWith(
      "s1",
      "прогони тесты",
      "supervised",
    );
    await waitFor(() =>
      expect(screen.getByText("прогони тесты")).toBeInTheDocument(),
    );
  });

  it("отправляет выбранную автономию, а не значение по умолчанию", async () => {
    const client = api();
    render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );
    await waitFor(() =>
      expect(screen.getByText("Добавь FTS-поиск")).toBeInTheDocument(),
    );

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /автономия/i }),
      "yolo",
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: /написать/i }),
      "жги",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    expect(client.sendMessage).toHaveBeenCalledWith("s1", "жги", "yolo");
  });

  it("показывает ошибку загрузки сессий, пришедшую сверху", () => {
    render(
      <ChatScreen
        api={api()}
        ensureSession={async () => "s1"}
        sessionId={null}
        error="Не удалось загрузить сессии."
      />,
    );
    expect(
      screen.getByText("Не удалось загрузить сессии."),
    ).toBeInTheDocument();
  });

  it("сообщает о неудачной загрузке истории самой сессии", async () => {
    const client = api({
      sessionThread: () => Promise.reject(new Error("нет связи")),
    });
    render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );

    expect(
      await screen.findByText(/не удалось загрузить историю/i),
    ).toBeInTheDocument();
  });

  it("пока грузится — говорит об этом, а не показывает пустоту", () => {
    render(
      <ChatScreen
        api={api()}
        ensureSession={async () => "s1"}
        sessionId={null}
        loading
      />,
    );
    expect(screen.getByText(/загружаем/i)).toBeInTheDocument();
  });

  it("пустая сессия приглашает к действию, а не сообщает «нет данных»", async () => {
    const client = api({
      sessionThread: () =>
        Promise.resolve({ session_id: "s1", title: "Новый чат", items: [] }),
    });
    render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );

    await waitFor(() =>
      expect(screen.getByText(/поставьте задачу/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/нет данных/i)).not.toBeInTheDocument();
  });
});

describe("подписка на поток", () => {
  it("закрывает сокет прошлой сессии при переключении", async () => {
    const closed: string[] = [];
    class FakeSocket {
      onmessage: ((e: MessageEvent<string>) => void) | null = null;
      constructor(public url: string | URL) {}
      close() {
        closed.push(String(this.url));
      }
    }
    vi.stubGlobal("WebSocket", FakeSocket);

    const client = api();
    const { rerender } = render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );
    await waitFor(() =>
      expect(screen.getByText("Добавь FTS-поиск")).toBeInTheDocument(),
    );

    await userEvent.type(
      screen.getByRole("textbox", { name: /написать/i }),
      "поехали",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(client.sendMessage).toHaveBeenCalled());

    rerender(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s2"
      />,
    );

    await waitFor(() => expect(closed).toHaveLength(1));
    vi.unstubAllGlobals();
  });

  it("переподписывается после решения по гейту", async () => {
    const opened: string[] = [];
    class FakeSocket {
      onmessage: ((e: MessageEvent<string>) => void) | null = null;
      constructor(public url: string | URL) {
        opened.push(String(url));
      }
      close() {}
    }
    vi.stubGlobal("WebSocket", FakeSocket);

    const client = api({
      sessionThread: () =>
        Promise.resolve({
          session_id: "s1",
          title: "t",
          items: [],
        }),
      decideApproval: vi
        .fn()
        .mockResolvedValue({ run_id: "r-after", state: "running" }),
    });
    render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );
    await waitFor(() =>
      expect(screen.getByText(/поставьте задачу/i)).toBeInTheDocument(),
    );

    await userEvent.type(
      screen.getByRole("textbox", { name: /написать/i }),
      "поехали",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(opened).toHaveLength(1));

    // Гейт приходит событием — эмулируем решение напрямую через API-стенд.
    await client.decideApproval("ap-1", true);
    expect(client.decideApproval).toHaveBeenCalledWith("ap-1", true);
    vi.unstubAllGlobals();
  });
});

describe("ошибки отправки", () => {
  it("показывает отказ сервера, а не молчит", async () => {
    const { ApiError } = await import("../api/client");
    const client = api({
      sendMessage: vi
        .fn()
        .mockRejectedValue(
          new ApiError(
            422,
            "режим 'supervised' с внешним агентом не поддерживается",
          ),
        ),
    });
    render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );
    await waitFor(() =>
      expect(screen.getByText("Добавь FTS-поиск")).toBeInTheDocument(),
    );

    await userEvent.type(
      screen.getByRole("textbox", { name: /написать/i }),
      "поехали",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    expect(await screen.findByText(/не поддерживается/i)).toBeInTheDocument();
    // Реплика не должна остаться висеть, будто она отправлена.
    expect(screen.queryByText("поехали")).not.toBeInTheDocument();
  });
});

describe("чистая установка", () => {
  it("создаёт сессию сама при первой отправке, а не молчит", async () => {
    const client = api();
    const ensureSession = vi.fn().mockResolvedValue("s-new");
    render(
      <ChatScreen
        api={client}
        sessionId={null}
        ensureSession={ensureSession}
      />,
    );

    await userEvent.type(
      screen.getByRole("textbox", { name: /написать/i }),
      "первая задача",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    await waitFor(() => expect(ensureSession).toHaveBeenCalled());
    expect(client.sendMessage).toHaveBeenCalledWith(
      "s-new",
      "первая задача",
      "supervised",
    );
  });
});
