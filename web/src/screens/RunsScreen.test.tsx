import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Api } from "../api/client";
import { fakeApi } from "../test/fakeApi";
import { RunsScreen } from "./RunsScreen";

const summary = {
  run_id: "r-8f3a91",
  state: "completed",
  task: "FTS-поиск по памяти",
  autonomy: "supervised",
  iterations: 7,
  tokens_used: 18400,
  cost_usd: 0.12,
  error: null,
};

const api = (over: Partial<Api> = {}): Api =>
  fakeApi({
    runs: () =>
      Promise.resolve([
        summary,
        {
          ...summary,
          run_id: "r-7c02de",
          state: "failed",
          task: "Прогон сценариев",
        },
      ]),
    run: () =>
      Promise.resolve({
        ...summary,
        messages: [],
        tool_calls: [
          {
            tool_name: "write_file",
            risk_level: "medium",
            policy_decision: "allow",
            status: "succeeded",
            error: null,
          },
        ],
        checks: [],
      }),
    runDiff: () =>
      Promise.resolve({
        run_id: summary.run_id,
        committed: "--- a/x.py\n+++ b/x.py\n+добавлено\n-удалено",
        uncommitted: "",
      }),
    ...over,
  });

describe("экран запусков", () => {
  it("показывает список с состоянием и счётчиками", async () => {
    render(<RunsScreen api={api()} />);

    expect(await screen.findByText("завершён")).toBeInTheDocument();
    expect(screen.getByText("упал")).toBeInTheDocument();
    expect(screen.getAllByText(/7 шагов · 18400 токенов/)).toHaveLength(2);
  });

  it("открывает трейс с вызовами и диффом", async () => {
    render(<RunsScreen api={api()} />);
    await screen.findByText("завершён");

    await userEvent.click(
      screen.getByRole("button", { name: /FTS-поиск по памяти/ }),
    );

    expect(await screen.findByText("r-8f3a91")).toBeInTheDocument();
    expect(screen.getByText("write_file")).toBeInTheDocument();
    expect(await screen.findByText("Закоммичено")).toBeInTheDocument();
    expect(screen.getByText("+добавлено")).toBeInTheDocument();
  });

  it("говорит, когда запуск ничего не изменил", async () => {
    const client = api({
      runDiff: () =>
        Promise.resolve({ run_id: "r-8f3a91", committed: "", uncommitted: "" }),
    });
    render(<RunsScreen api={client} />);
    await screen.findByText("завершён");

    await userEvent.click(
      screen.getByRole("button", { name: /FTS-поиск по памяти/ }),
    );

    expect(await screen.findByText(/ничего не изменил/i)).toBeInTheDocument();
  });

  it("на пустом списке зовёт поставить задачу", async () => {
    render(<RunsScreen api={fakeApi()} />);
    expect(
      await screen.findByText(/поставьте первую задачу/i),
    ).toBeInTheDocument();
  });

  it("сообщает об ошибке загрузки", async () => {
    const client = fakeApi({
      runs: vi.fn().mockRejectedValue(new Error("нет связи")),
    });
    render(<RunsScreen api={client} />);
    expect(
      await screen.findByText(/не удалось загрузить запуски/i),
    ).toBeInTheDocument();
  });
});
