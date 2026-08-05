import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { SessionSummary } from "../api/types";
import { Nav } from "./Nav";

function session(
  id: string,
  workspace: string | null,
  updatedAt: string,
): SessionSummary {
  return {
    session_id: id,
    title: `Чат ${id}`,
    workspace,
    root: workspace,
    updated_at: updatedAt,
    runs_count: 1,
    last_state: "completed",
  };
}

const noop = () => {};

describe("шапка навигатора", () => {
  it("вместо текста — пиксельный логотип с доступным именем «Сварог»", () => {
    render(
      <Nav
        sessions={[]}
        activeId={null}
        onPick={noop}
        onNew={noop}
        onDelete={noop}
        section="chat"
        onSection={noop}
      />,
    );
    expect(screen.getByRole("img", { name: "Сварог" })).toBeInTheDocument();
  });
});

describe("группировка чатов по папкам", () => {
  it("секции — папки в порядке свежести, внутри — по времени", () => {
    render(
      <Nav
        sessions={[
          session("a", "/home/u/proj/TaskTracker", "2026-08-04T12:00:00"),
          session("b", "/home/u/proj/Svarog", "2026-08-04T11:00:00"),
          session("c", "/home/u/proj/TaskTracker", "2026-08-03T10:00:00"),
        ]}
        activeId={null}
        onPick={noop}
        onNew={noop}
        onDelete={noop}
        section="chat"
        onSection={noop}
      />,
    );

    const headers = screen.getAllByTestId("nav-group");
    expect(headers.map((h) => h.textContent)).toEqual([
      "TaskTracker",
      "Svarog",
    ]);
    // Полный путь — подсказкой на заголовке секции.
    expect(headers[0]).toHaveAttribute("title", "/home/u/proj/TaskTracker");
    // Бейдж папки в строках больше не нужен: папка уже в заголовке секции.
    expect(document.querySelector(".nav__root")).toBeNull();
  });

  it("чаты без папки собираются в «Без папки»", () => {
    render(
      <Nav
        sessions={[
          session("a", "/home/u/proj/TaskTracker", "2026-08-04T12:00:00"),
          session("b", null, "2026-08-04T11:00:00"),
        ]}
        activeId={null}
        onPick={noop}
        onNew={noop}
        onDelete={noop}
        section="chat"
        onSection={noop}
      />,
    );

    const headers = screen.getAllByTestId("nav-group");
    expect(headers.map((h) => h.textContent)).toEqual([
      "TaskTracker",
      "Без папки",
    ]);
    const list = screen.getByTestId("nav-list");
    expect(within(list).getByText("Чат b")).toBeInTheDocument();
  });

  it("клик по заголовку сворачивает и разворачивает папку", async () => {
    const user = userEvent.setup();
    render(
      <Nav
        sessions={[
          session("a", "/home/u/proj/TaskTracker", "2026-08-04T12:00:00"),
          session("b", "/home/u/proj/Svarog", "2026-08-04T11:00:00"),
        ]}
        activeId={null}
        onPick={noop}
        onNew={noop}
        onDelete={noop}
        section="chat"
        onSection={noop}
      />,
    );

    const header = screen.getAllByTestId("nav-group")[0];
    expect(header).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Чат a")).toBeInTheDocument();

    await user.click(header);
    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Чат a")).not.toBeInTheDocument();
    // Соседняя папка не пострадала.
    expect(screen.getByText("Чат b")).toBeInTheDocument();

    await user.click(header);
    expect(screen.getByText("Чат a")).toBeInTheDocument();
  });

  it("свёрнутость папок переживает перерисовку через localStorage", async () => {
    window.localStorage.setItem(
      "svarog.navCollapsed",
      JSON.stringify(["/home/u/proj/TaskTracker"]),
    );
    render(
      <Nav
        sessions={[
          session("a", "/home/u/proj/TaskTracker", "2026-08-04T12:00:00"),
          session("b", "/home/u/proj/Svarog", "2026-08-04T11:00:00"),
        ]}
        activeId={null}
        onPick={noop}
        onNew={noop}
        onDelete={noop}
        section="chat"
        onSection={noop}
      />,
    );

    expect(screen.queryByText("Чат a")).not.toBeInTheDocument();
    expect(screen.getByText("Чат b")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getAllByTestId("nav-group")[0]);
    expect(
      JSON.parse(window.localStorage.getItem("svarog.navCollapsed") ?? "[]"),
    ).toEqual([]);
  });

  it("одноимённые папки из разных путей — отдельные секции", () => {
    render(
      <Nav
        sessions={[
          session("a", "/home/u/work/api", "2026-08-04T12:00:00"),
          session("b", "/home/u/pet/api", "2026-08-04T11:00:00"),
        ]}
        activeId={null}
        onPick={noop}
        onNew={noop}
        onDelete={noop}
        section="chat"
        onSection={noop}
      />,
    );

    const headers = screen.getAllByTestId("nav-group");
    expect(headers).toHaveLength(2);
    expect(headers[0]).toHaveAttribute("title", "/home/u/work/api");
    expect(headers[1]).toHaveAttribute("title", "/home/u/pet/api");
  });
});
