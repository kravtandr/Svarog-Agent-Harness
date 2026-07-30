import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fakeApi } from "../test/fakeApi";
import { WorkspacePicker } from "./WorkspacePicker";

function setup() {
  const api = fakeApi({
    fsRecent: vi.fn().mockResolvedValue([
      {
        path: "/home/u/proj/жив",
        exists: true,
        last_used: "2026-07-30T10:00:00Z",
      },
      {
        path: "/home/u/proj/умер",
        exists: false,
        last_used: "2026-07-29T10:00:00Z",
      },
    ]),
    fs: vi.fn().mockResolvedValue({
      path: "/home/u",
      parent: "/home",
      entries: [
        { name: "proj", path: "/home/u/proj", accessible: true },
        { name: "закрыто", path: "/home/u/закрыто", accessible: false },
      ],
    }),
  });
  const onPick = vi.fn().mockResolvedValue(undefined);
  const onCancel = vi.fn();
  render(<WorkspacePicker api={api} onPick={onPick} onCancel={onCancel} />);
  return { api, onPick, onCancel };
}

describe("WorkspacePicker", () => {
  it("недавние: живой выбирается кликом, мёртвый заблокирован", async () => {
    const { onPick } = setup();
    const alive = await screen.findByRole("button", {
      name: "/home/u/proj/жив",
    });
    await userEvent.click(alive);
    expect(onPick).toHaveBeenCalledWith("/home/u/proj/жив");
    expect(
      screen.getByRole("button", { name: "/home/u/proj/умер" }),
    ).toBeDisabled();
  });

  it("обзор: клик по каталогу спускается, кнопка выбирает текущий", async () => {
    const { api, onPick } = setup();
    await userEvent.click(await screen.findByRole("button", { name: "proj" }));
    await waitFor(() => expect(api.fs).toHaveBeenCalledWith("/home/u/proj"));
    await userEvent.click(
      screen.getByRole("button", { name: "Выбрать эту папку" }),
    );
    expect(onPick).toHaveBeenCalled();
  });

  it("ввод пути: подсказки по префиксу, Enter подтверждает введённое", async () => {
    const { api, onPick } = setup();
    const field = await screen.findByRole("combobox", { name: "Путь к папке" });
    await userEvent.type(field, "/home/u/pr");
    await waitFor(() => expect(api.fs).toHaveBeenCalledWith("/home/u"));
    // Раздел «Обзор» тоже показывает «proj» (тот же домашний листинг) —
    // берём текст именно из списка подсказок автодополнения, а не из
    // первого совпадения на всей странице.
    const suggestions = await screen.findByRole("listbox", {
      name: "Подсказки ввода",
    });
    expect(within(suggestions).getByText("proj")).toBeInTheDocument();
    await userEvent.clear(field);
    await userEvent.type(field, "/home/u/proj{Enter}");
    expect(onPick).toHaveBeenCalledWith("/home/u/proj");
  });

  it("ошибка создания рисуется инлайн", async () => {
    const { onPick } = setup();
    onPick.mockRejectedValueOnce(
      new Error("не каталог или не существует: /home/u/proj/жив"),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "/home/u/proj/жив" }),
    );
    expect(
      await screen.findByText("не каталог или не существует: /home/u/proj/жив"),
    ).toBeInTheDocument();
  });

  it("кнопка отмены зовёт onCancel", async () => {
    const { onCancel } = setup();
    await userEvent.click(
      await screen.findByRole("button", { name: "Отмена" }),
    );
    expect(onCancel).toHaveBeenCalled();
  });
});

describe("диалог пересечения с control-plane", () => {
  function setupOverlap() {
    const api = fakeApi({
      fsRecent: vi.fn().mockResolvedValue([
        {
          path: "/home/u/proj/жив",
          exists: true,
          last_used: "2026-07-30T10:00:00Z",
        },
      ]),
      fsInspect: vi.fn().mockImplementation((path: string) =>
        Promise.resolve({
          path,
          overlap_warnings: [
            "storage.db_path (…) пересекается с workspace (…)",
          ],
          blocking: true,
        }),
      ),
    });
    const onPick = vi.fn().mockResolvedValue(undefined);
    const onCancel = vi.fn();
    render(<WorkspacePicker api={api} onPick={onPick} onCancel={onCancel} />);
    return { api, onPick, onCancel };
  }

  it("блокирующее пересечение показывает диалог вместо создания чата", async () => {
    const { onPick } = setupOverlap();
    await userEvent.click(
      await screen.findByRole("button", { name: "/home/u/proj/жив" }),
    );
    expect(
      await screen.findByText("В этой папке живут данные Сварога"),
    ).toBeInTheDocument();
    expect(onPick).not.toHaveBeenCalled();
  });

  it("«Принять риски» создаёт чат с accept_overlap", async () => {
    const { onPick } = setupOverlap();
    await userEvent.click(
      await screen.findByRole("button", { name: "/home/u/proj/жив" }),
    );
    await userEvent.click(
      await screen.findByRole("button", {
        name: "Принять риски и продолжить",
      }),
    );
    expect(onPick).toHaveBeenCalledWith("/home/u/proj/жив", true);
  });

  it("«Выбрать другую папку» возвращает к пикеру без создания", async () => {
    const { onPick } = setupOverlap();
    await userEvent.click(
      await screen.findByRole("button", { name: "/home/u/proj/жив" }),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "Выбрать другую папку" }),
    );
    expect(
      screen.queryByText("В этой папке живут данные Сварога"),
    ).not.toBeInTheDocument();
    expect(await screen.findByText("Где работать?")).toBeInTheDocument();
    expect(onPick).not.toHaveBeenCalled();
  });
});
