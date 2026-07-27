import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type Api } from "../api/client";
import { fakeApi as baseApi } from "../test/fakeApi";
import { MemoryScreen } from "./MemoryScreen";

const fakeApi = (over: Partial<Api> = {}): Api =>
  baseApi({
    memoryTree: vi.fn().mockResolvedValue([
      {
        path: "профиль.md",
        size_bytes: 120,
        modified_at: "2026-07-27T10:00:00Z",
      },
      {
        path: "решения/fts.md",
        size_bytes: 900,
        modified_at: "2026-07-27T11:00:00Z",
      },
    ]),
    memoryFile: vi.fn().mockResolvedValue({
      path: "профиль.md",
      text: "# Профиль\n\nАндрей, Svarog.",
      size_bytes: 120,
      modified_at: "2026-07-27T10:00:00Z",
    }),
    memorySearch: vi
      .fn()
      .mockResolvedValue([
        { path: "решения/fts.md", snippet: "точный проход раньше широкого" },
      ]),
    ...over,
  });

describe("экран памяти", () => {
  it("показывает дерево страниц и открывает первую", async () => {
    render(<MemoryScreen api={fakeApi()} />);

    expect(await screen.findByText("2 записи")).toBeInTheDocument();
    expect(screen.getByText("memory/профиль.md")).toBeInTheDocument();
    expect(screen.getByText(/Андрей, Svarog/)).toBeInTheDocument();
  });

  it("ищет через search_memory и показывает фрагменты", async () => {
    const api = fakeApi();
    render(<MemoryScreen api={api} />);
    await screen.findByText("2 записи");

    await userEvent.type(
      screen.getByRole("searchbox", { name: /поиск/i }),
      "префикс",
    );

    await waitFor(() =>
      expect(api.memorySearch).toHaveBeenCalledWith("префикс"),
    );
    expect(await screen.findByText("Найдено в 1 записи")).toBeInTheDocument();
    expect(
      screen.getByText("точный проход раньше широкого"),
    ).toBeInTheDocument();
  });

  it("открывает страницу по нажатию", async () => {
    const api = fakeApi();
    render(<MemoryScreen api={api} />);
    await screen.findByText("2 записи");

    await userEvent.click(
      screen.getByRole("button", { name: /решения\/fts\.md/ }),
    );

    expect(api.memoryFile).toHaveBeenCalledWith("решения/fts.md");
  });

  it("объясняет, что память не настроена, а не падает", async () => {
    const api = fakeApi({
      memoryTree: vi
        .fn()
        .mockRejectedValue(new ApiError(404, "память не настроена")),
    });
    render(<MemoryScreen api={api} />);

    expect(await screen.findByText(/память не настроена/i)).toBeInTheDocument();
  });
});
