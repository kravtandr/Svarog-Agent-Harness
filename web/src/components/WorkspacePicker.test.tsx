import { render, screen, waitFor } from "@testing-library/react";
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
    expect(await screen.findByText("proj")).toBeInTheDocument();
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
