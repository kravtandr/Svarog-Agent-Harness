import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { App } from "./App";
import { fakeApi } from "./test/fakeApi";

const session = {
  session_id: "s1",
  title: "FTS-поиск по памяти",
  workspace: null,
  updated_at: new Date().toISOString(),
  runs_count: 1,
  last_state: "completed",
};

const api = () => fakeApi({ listSessions: () => Promise.resolve([session]) });

describe("оболочка приложения", () => {
  it("показывает сессии и открывает диалог по умолчанию", async () => {
    render(<App api={api()} />);

    expect(
      await screen.findByRole("button", { name: "FTS-поиск по памяти" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("textbox", { name: /написать/i }),
    ).toBeInTheDocument();
  });

  it("переключает разделы, у которых есть экран", async () => {
    render(<App api={api()} />);
    await screen.findByRole("button", { name: "FTS-поиск по памяти" });

    await userEvent.click(screen.getByRole("button", { name: "Настройки" }));
    expect(
      await screen.findByText(/загружаем настройки|секреты/i),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Память" }));
    expect(
      await screen.findByText(/записей|память не настроена|загружаем память/i),
    ).toBeInTheDocument();
  });

  it("все разделы навигатора открываются", async () => {
    render(<App api={api()} />);
    await screen.findByRole("button", { name: "FTS-поиск по памяти" });

    for (const [title, marker] of [
      ["Запуски", /поставьте первую задачу|загружаем запуски/i],
      ["Скиллы", /положите их в каталог skills|загружаем скиллы/i],
      ["Память", /записей|память не настроена|загружаем память/i],
      ["Настройки", /секреты|загружаем настройки/i],
    ] as const) {
      const button = screen.getByRole("button", { name: title });
      expect(button).toBeEnabled();
      await userEvent.click(button);
      expect(await screen.findByText(marker)).toBeInTheDocument();
    }
  });

  it("сообщает о недоступном сервере, а не показывает пустоту", async () => {
    const client = fakeApi({
      listSessions: () => Promise.reject(new Error("нет связи")),
    });
    render(<App api={client} />);

    await waitFor(() =>
      expect(
        screen.getByText(/не удалось загрузить сессии/i),
      ).toBeInTheDocument(),
    );
  });

  it("создаёт новый чат и возвращает в диалог", async () => {
    const client = api();
    render(<App api={client} />);
    await screen.findByRole("button", { name: "FTS-поиск по памяти" });

    await userEvent.click(screen.getByRole("button", { name: /Новый чат/ }));

    expect(client.createSession).toHaveBeenCalledWith("Новый чат");
  });
});
