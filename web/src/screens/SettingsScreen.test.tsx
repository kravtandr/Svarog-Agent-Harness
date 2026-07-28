import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Api } from "../api/client";
import type { ConfigView } from "../api/types";
import { fakeApi as baseApi } from "../test/fakeApi";
import { SettingsScreen } from "./SettingsScreen";

const config: ConfigView = {
  path: "/agent-home/svarog.yaml",
  sections: [
    {
      key: "policies",
      title: "Политики и автономия",
      fields: [
        {
          path: "runtime.autonomy",
          label: "Уровень автономии",
          help: "Как поступать с действиями среднего риска.",
          kind: "enum",
          value: "yolo",
          choices: ["supervised", "auto", "yolo"],
          minimum: null,
          maximum: null,
        },
        {
          path: "runtime.max_iterations",
          label: "Максимум шагов в одном запуске",
          help: "",
          kind: "int",
          value: 50,
          choices: [],
          minimum: 0,
          maximum: null,
        },
        {
          path: "git.require_approval_for_push",
          label: "Спрашивать перед push",
          help: "",
          kind: "bool",
          value: true,
          choices: [],
          minimum: null,
          maximum: null,
        },
      ],
    },
  ],
};

const fakeApi = (over: Partial<Api> = {}): Api =>
  baseApi({
    config: vi.fn().mockResolvedValue(config),
    previewConfig: vi.fn().mockResolvedValue({
      path: config.path,
      changes: 2,
      lines: [
        { kind: "same", text: "runtime:" },
        { kind: "del", text: "  autonomy: yolo" },
        { kind: "add", text: "  autonomy: supervised" },
      ],
      restart_required: false,
    }),
    saveConfig: vi.fn().mockResolvedValue({
      path: config.path,
      changes: 0,
      lines: [],
      restart_required: false,
    }),
    secrets: vi.fn().mockResolvedValue([
      { name: "PROVIDER_API_KEY", present: true },
      { name: "GITHUB_TOKEN", present: false },
    ]),
    ...over,
  });

describe("экран настроек", () => {
  it("строит форму из ответа сервера", async () => {
    render(<SettingsScreen api={fakeApi()} />);

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Политики и автономия" }),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("combobox", { name: /уровень автономии/i }),
    ).toHaveValue("yolo");
    expect(
      screen.getByRole("spinbutton", { name: /максимум шагов/i }),
    ).toHaveValue(50);
    expect(
      screen.getByRole("checkbox", { name: /спрашивать перед push/i }),
    ).toBeChecked();
  });

  it("показывает дифф файла после правки и не сохраняет сам", async () => {
    const api = fakeApi();
    render(<SettingsScreen api={api} />);
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Политики и автономия" }),
      ).toBeInTheDocument(),
    );

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /уровень автономии/i }),
      "supervised",
    );

    await waitFor(() =>
      expect(api.previewConfig).toHaveBeenCalledWith({
        "runtime.autonomy": "supervised",
      }),
    );
    const pane = screen.getByTestId("diffpane");
    await waitFor(() => expect(pane).toHaveTextContent("autonomy: supervised"));
    // Добавленная строка помечена как добавленная, а не просто выведена.
    const added = pane.querySelectorAll(".diffpane__line--add");
    expect([...added].map((node) => node.textContent)).toEqual([
      "+  autonomy: supervised",
    ]);
    expect(api.saveConfig).not.toHaveBeenCalled();
  });

  it("сохраняет только по нажатию и сообщает число изменений", async () => {
    const api = fakeApi();
    render(<SettingsScreen api={api} />);
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Политики и автономия" }),
      ).toBeInTheDocument(),
    );

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /уровень автономии/i }),
      "supervised",
    );
    await waitFor(() =>
      expect(screen.getByText(/2 изменения/)).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    expect(api.saveConfig).toHaveBeenCalledWith({
      "runtime.autonomy": "supervised",
    });
  });

  it("показывает отказ схемы на месте, а не общим сообщением", async () => {
    const { ApiError } = await import("../api/client");
    const api = fakeApi({
      previewConfig: vi
        .fn()
        .mockRejectedValue(
          new ApiError(422, "max_iterations: должно быть > 0"),
        ),
    });
    render(<SettingsScreen api={api} />);
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Политики и автономия" }),
      ).toBeInTheDocument(),
    );

    const steps = screen.getByRole("spinbutton", { name: /максимум шагов/i });
    await userEvent.clear(steps);
    await userEvent.type(steps, "0");

    await waitFor(() =>
      expect(screen.getByText(/должно быть > 0/)).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Сохранить" })).toBeDisabled();
  });

  it("сообщает, что правка вступит в силу после текущих запусков", async () => {
    const api = fakeApi({
      saveConfig: vi.fn().mockResolvedValue({
        path: config.path,
        changes: 0,
        lines: [],
        restart_required: true,
      }),
    });
    render(<SettingsScreen api={api} />);
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Политики и автономия" }),
      ).toBeInTheDocument(),
    );

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /уровень автономии/i }),
      "supervised",
    );
    await waitFor(() =>
      expect(screen.getByText(/2 изменения/)).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() =>
      expect(
        screen.getByText(/вступит в силу.*текущ.*запуск/i),
      ).toBeInTheDocument(),
    );
  });

  it("не показывает эту заметку, когда перезапуск не нужен", async () => {
    const api = fakeApi();
    render(<SettingsScreen api={api} />);
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Политики и автономия" }),
      ).toBeInTheDocument(),
    );

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /уровень автономии/i }),
      "supervised",
    );
    await waitFor(() =>
      expect(screen.getByText(/2 изменения/)).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(api.saveConfig).toHaveBeenCalled());
    expect(
      screen.queryByText(/вступит в силу.*текущ.*запуск/i),
    ).not.toBeInTheDocument();
  });

  it("показывает имена секретов без значений", async () => {
    render(<SettingsScreen api={fakeApi()} />);

    await userEvent.click(
      await screen.findByRole("button", { name: /секреты/i }),
    );

    expect(await screen.findByText("PROVIDER_API_KEY")).toBeInTheDocument();
    expect(screen.getByText("задан")).toBeInTheDocument();
    expect(screen.getByText("не задан")).toBeInTheDocument();
  });
});
