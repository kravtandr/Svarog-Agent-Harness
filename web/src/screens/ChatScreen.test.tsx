import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

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
    render(<ChatScreen api={api()} sessionId="s1" />);

    await waitFor(() =>
      expect(screen.getByText("Добавь FTS-поиск")).toBeInTheDocument(),
    );
    expect(screen.getByText("write_file")).toBeInTheDocument();
    expect(screen.getByText("записано 1234 символов")).toBeInTheDocument();
  });

  it("отправляет сообщение в текущую сессию", async () => {
    const client = api();
    render(<ChatScreen api={client} sessionId="s1" />);
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
    render(<ChatScreen api={client} sessionId="s1" />);
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
    render(<ChatScreen api={client} sessionId="s1" />);

    expect(
      await screen.findByText(/не удалось загрузить историю/i),
    ).toBeInTheDocument();
  });

  it("пока грузится — говорит об этом, а не показывает пустоту", () => {
    render(<ChatScreen api={api()} sessionId={null} loading />);
    expect(screen.getByText(/загружаем/i)).toBeInTheDocument();
  });

  it("пустая сессия приглашает к действию, а не сообщает «нет данных»", async () => {
    const client = api({
      sessionThread: () =>
        Promise.resolve({ session_id: "s1", title: "Новый чат", items: [] }),
    });
    render(<ChatScreen api={client} sessionId="s1" />);

    await waitFor(() =>
      expect(screen.getByText(/поставьте задачу/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/нет данных/i)).not.toBeInTheDocument();
  });
});
