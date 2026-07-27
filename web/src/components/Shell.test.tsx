import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { SessionSummary } from "../api/types";
import { heatLevel, Nav } from "./Nav";
import { Shell } from "./Shell";

const session = (over: Partial<SessionSummary>): SessionSummary => ({
  session_id: "s1",
  title: "FTS-поиск по памяти",
  workspace: null,
  updated_at: new Date().toISOString(),
  runs_count: 1,
  last_state: "completed",
  ...over,
});

describe("навигатор", () => {
  it("показывает сессии и разделы", () => {
    render(
      <Nav
        sessions={[session({})]}
        activeId="s1"
        onPick={() => {}}
        onNew={() => {}}
        onDelete={() => {}}
        section="chat"
        onSection={() => {}}
      />,
    );
    expect(screen.getByText("FTS-поиск по памяти")).toBeInTheDocument();
    expect(screen.getByText("Скиллы")).toBeInTheDocument();
    expect(screen.getByText("Память")).toBeInTheDocument();
    expect(screen.getByText("Настройки")).toBeInTheDocument();
  });

  it("сообщает выбор сессии", async () => {
    const onPick = vi.fn();
    render(
      <Nav
        sessions={[session({})]}
        activeId={null}
        onPick={onPick}
        onNew={() => {}}
        onDelete={() => {}}
        section="chat"
        onSection={() => {}}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "FTS-поиск по памяти" }),
    );
    expect(onPick).toHaveBeenCalledWith("s1");
  });

  it("красит шкалу накала по свежести", () => {
    const day = 24 * 60 * 60 * 1000;
    render(
      <Nav
        sessions={[
          session({ session_id: "live", title: "идёт", last_state: "running" }),
          session({ session_id: "fresh", title: "свежая" }),
          session({
            session_id: "old",
            title: "старая",
            updated_at: new Date(Date.now() - 8 * day).toISOString(),
          }),
        ]}
        activeId={null}
        onPick={() => {}}
        onNew={() => {}}
        onDelete={() => {}}
        section="chat"
        onSection={() => {}}
      />,
    );
    expect(screen.getByTestId("heat-live")).toHaveAttribute("data-heat", "0");
    expect(screen.getByTestId("heat-fresh")).toHaveAttribute("data-heat", "1");
    expect(screen.getByTestId("heat-old")).toHaveAttribute("data-heat", "4");
  });
});

describe("оболочка", () => {
  it("открывает и закрывает выдвижной навигатор", async () => {
    render(
      <Shell nav={<div>навигатор</div>} bar={<div>шапка</div>}>
        <div>лента</div>
      </Shell>,
    );

    expect(screen.getByTestId("shell-nav")).toHaveAttribute(
      "data-open",
      "false",
    );
    await userEvent.click(
      screen.getByRole("button", { name: /показать навигатор/i }),
    );
    expect(screen.getByTestId("shell-nav")).toHaveAttribute(
      "data-open",
      "true",
    );
    await userEvent.click(screen.getByTestId("shell-scrim"));
    expect(screen.getByTestId("shell-nav")).toHaveAttribute(
      "data-open",
      "false",
    );
  });
});

describe("шкала накала и время", () => {
  it("трактует наивное время сервера как UTC, а не как локальное", () => {
    // Сервер отдаёт «2026-07-27T12:00:00» без зоны. Час назад по UTC — это
    // уровень 1, независимо от часового пояса зрителя.
    const now = Date.parse("2026-07-27T13:00:00Z");
    const session: SessionSummary = {
      session_id: "s",
      title: "t",
      workspace: null,
      updated_at: "2026-07-27T12:30:00",
      runs_count: 1,
      last_state: "completed",
    };
    expect(heatLevel(session, now)).toBe(1);
  });

  it("не путается, когда зона всё-таки указана", () => {
    const now = Date.parse("2026-07-27T13:00:00Z");
    const session: SessionSummary = {
      session_id: "s",
      title: "t",
      workspace: null,
      updated_at: "2026-07-27T12:30:00Z",
      runs_count: 1,
      last_state: "completed",
    };
    expect(heatLevel(session, now)).toBe(1);
  });
});

describe("занятость и удаление чата", () => {
  const busy = (state: string): SessionSummary =>
    session({ session_id: "b", title: "занятый", last_state: state });

  it("показывает, что в чате идёт работа", () => {
    render(
      <Nav
        sessions={[busy("running")]}
        activeId={null}
        onPick={() => {}}
        onNew={() => {}}
        onDelete={() => {}}
        section="chat"
        onSection={() => {}}
      />,
    );
    expect(screen.getByText("идёт")).toBeInTheDocument();
  });

  it("различает ожидание решения и завершённый чат", () => {
    const { rerender } = render(
      <Nav
        sessions={[busy("waiting_approval")]}
        activeId={null}
        onPick={() => {}}
        onNew={() => {}}
        onDelete={() => {}}
        section="chat"
        onSection={() => {}}
      />,
    );
    expect(screen.getByText("ждёт решения")).toBeInTheDocument();

    rerender(
      <Nav
        sessions={[busy("completed")]}
        activeId={null}
        onPick={() => {}}
        onNew={() => {}}
        onDelete={() => {}}
        section="chat"
        onSection={() => {}}
      />,
    );
    expect(screen.queryByText(/идёт|ждёт решения/)).not.toBeInTheDocument();
  });

  it("сообщает об удалении, не трогая выбор чата", async () => {
    const onDelete = vi.fn();
    const onPick = vi.fn();
    render(
      <Nav
        sessions={[session({})]}
        activeId={null}
        onPick={onPick}
        onNew={() => {}}
        onDelete={onDelete}
        section="chat"
        onSection={() => {}}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: /удалить чат/i }));

    expect(onDelete).toHaveBeenCalledWith("s1");
    expect(onPick).not.toHaveBeenCalled();
  });
});
