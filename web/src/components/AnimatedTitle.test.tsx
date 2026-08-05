import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AnimatedTitle } from "./AnimatedTitle";

describe("AnimatedTitle", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("первый маунт рендерит текст сразу, без анимации", () => {
    render(<AnimatedTitle text="Готовое название" />);
    expect(screen.getByText("Готовое название")).toBeInTheDocument();
  });

  it("смена текста печатается посимвольно до конца", () => {
    vi.useFakeTimers();
    const { rerender } = render(<AnimatedTitle text="Старое" />);
    rerender(<AnimatedTitle text="Новое имя" />);
    // Спустя два тика напечатана только часть.
    act(() => {
      vi.advanceTimersByTime(2 * 25);
    });
    expect(screen.queryByText("Новое имя")).not.toBeInTheDocument();
    // Достаточно тиков — текст полный.
    act(() => {
      vi.advanceTimersByTime(25 * "Новое имя".length);
    });
    expect(screen.getByText("Новое имя")).toBeInTheDocument();
  });
});
