import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { fakeApi } from "./test/fakeApi";

const session = {
  session_id: "s1",
  title: "FTS-поиск по памяти",
  workspace: null,
  root: null,
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
      screen.getByRole("combobox", { name: /написать/i }),
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

  it("новый чат открывает пикер и создаёт сессию с выбранным путём", async () => {
    const client = fakeApi({
      fsRecent: vi.fn().mockResolvedValue([
        {
          path: "/home/u/proj",
          exists: true,
          last_used: "2026-07-30T10:00:00Z",
        },
      ]),
    });
    render(<App api={client} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "＋ Новый чат" }),
    );
    expect(client.createSession).not.toHaveBeenCalled();
    await userEvent.click(
      await screen.findByRole("button", { name: "/home/u/proj" }),
    );
    await waitFor(() =>
      expect(client.createSession).toHaveBeenCalledWith(
        "Новый чат",
        "/home/u/proj",
      ),
    );
  });

  it("отмена пикера возвращает в чат без создания сессии", async () => {
    const client = fakeApi();
    render(<App api={client} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "＋ Новый чат" }),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "Отмена" }),
    );
    expect(client.createSession).not.toHaveBeenCalled();
    expect(screen.queryByText("Где работать?")).not.toBeInTheDocument();
  });

  it("клик по существующему чату в навигаторе закрывает открытый пикер", async () => {
    const other = {
      session_id: "s2",
      title: "второй чат",
      workspace: null,
      root: null,
      updated_at: new Date().toISOString(),
      runs_count: 0,
      last_state: null,
    };
    const client = fakeApi({
      listSessions: () => Promise.resolve([session, other]),
    });
    render(<App api={client} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "＋ Новый чат" }),
    );
    expect(await screen.findByText("Где работать?")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "второй чат" }));

    expect(screen.queryByText("Где работать?")).not.toBeInTheDocument();
    expect(client.createSession).not.toHaveBeenCalled();
  });

  it("строка сессии показывает бейдж корня", async () => {
    const client = fakeApi({
      listSessions: vi.fn().mockResolvedValue([
        {
          session_id: "s1",
          title: "чат",
          workspace: "/home/u/proj/test",
          root: "/home/u/proj/test",
          updated_at: "2026-07-30T10:00:00Z",
          runs_count: 0,
          last_state: null,
        },
      ]),
    });
    render(<App api={client} />);
    // Бейдж есть и в шапке (активная сессия — единственная), поэтому строку
    // сессии проверяем адресно, внутри навигатора, а не по всему документу.
    const nav = screen.getByRole("navigation");
    expect(await within(nav).findByText("test")).toBeInTheDocument();
  });

  it("скоупит настройки/память/скиллы по root активной сессии, а не workspace", async () => {
    // repo/named-сессии: workspace — clone/task-каталог, root — корень
    // сервиса. Settings/Memory/Skills должны звать withRoot(root), иначе
    // X-Svarog-Root ведёт в мусорный «корень» и 422 (F4 финального ревью).
    const scoped = {
      session_id: "s1",
      title: "чат",
      workspace: "/home/u/proj/.svarog-tasks/clone-1",
      root: "/home/u/proj",
      updated_at: new Date().toISOString(),
      runs_count: 0,
      last_state: null,
    };
    const client = fakeApi({ listSessions: () => Promise.resolve([scoped]) });
    render(<App api={client} />);
    // Бейдж корня ("clone-1") входит в accessible name кнопки сессии, так
    // что точное имя "чат" не совпадёт, — ждём по частичному совпадению.
    await screen.findByRole("button", { name: /^чат/ });

    await userEvent.click(screen.getByRole("button", { name: "Настройки" }));

    expect(client.withRoot).toHaveBeenCalledWith("/home/u/proj");
  });

  it("не скоупит настройки, когда у активной сессии нет root (сессия до фичи)", async () => {
    const client = fakeApi({ listSessions: () => Promise.resolve([session]) });
    render(<App api={client} />);
    await screen.findByRole("button", { name: "FTS-поиск по памяти" });

    await userEvent.click(screen.getByRole("button", { name: "Настройки" }));

    expect(client.withRoot).not.toHaveBeenCalled();
  });

  it("команда /new в чате заводит новый чат так же, как кнопка навигатора", async () => {
    const client = fakeApi({
      listSessions: () => Promise.resolve([session]),
      fsRecent: vi.fn().mockResolvedValue([
        {
          path: "/home/u/proj",
          exists: true,
          last_used: "2026-07-30T10:00:00Z",
        },
      ]),
    });
    render(<App api={client} />);
    await screen.findByRole("button", { name: "FTS-поиск по памяти" });

    await userEvent.type(
      screen.getByRole("combobox", { name: /написать/i }),
      "/new{Enter}",
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "/home/u/proj" }),
    );
    await waitFor(() =>
      expect(client.createSession).toHaveBeenCalledWith(
        "Новый чат",
        "/home/u/proj",
      ),
    );
  });

  it("команда /sessions переводит фокус на навигатор", async () => {
    const client = api();
    render(<App api={client} />);
    await screen.findByRole("button", { name: "FTS-поиск по памяти" });

    await userEvent.type(
      screen.getByRole("combobox", { name: /написать/i }),
      "/sessions{Enter}",
    );

    expect(screen.getByRole("button", { name: /Новый чат/ })).toHaveFocus();
  });

  it("session_title из WS обновляет название в списке", async () => {
    class FakeSocket {
      static last: FakeSocket | null = null;
      onmessage: ((ev: { data: string }) => void) | null = null;
      onclose: (() => void) | null = null;
      constructor() {
        FakeSocket.last = this;
      }
      close() {}
    }
    vi.stubGlobal("WebSocket", FakeSocket);

    render(<App api={api()} />);
    await screen.findByRole("button", { name: "FTS-поиск по памяти" });

    act(() => {
      FakeSocket.last?.onmessage?.({
        data: JSON.stringify({
          type: "session_title",
          session_id: "s1",
          title: "Новое имя чата",
          phase: "draft",
        }),
      });
    });
    expect(
      await screen.findByRole("button", { name: /Новое имя чата/ }),
    ).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});
