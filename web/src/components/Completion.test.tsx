import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Completion, type CompletionItem } from "./Completion";

const ITEMS: CompletionItem[] = [
  { value: "/help", label: "/help", description: "показать команды" },
  { value: "/new", label: "/new", description: "новый чат" },
];

describe("Completion", () => {
  it("показывает значение и описание", () => {
    render(<Completion items={ITEMS} active={0} onPick={vi.fn()} />);
    expect(screen.getByText("/help")).toBeInTheDocument();
    expect(screen.getByText("показать команды")).toBeInTheDocument();
  });

  it("отмечает активную строку для программы чтения с экрана", () => {
    render(<Completion items={ITEMS} active={1} onPick={vi.fn()} />);
    expect(screen.getByText("/new").closest("li")).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("возвращает выбранное по клику", async () => {
    const onPick = vi.fn();
    render(<Completion items={ITEMS} active={0} onPick={onPick} />);
    await userEvent.click(screen.getByText("/new"));
    expect(onPick).toHaveBeenCalledWith("/new");
  });

  it("ничего не рисует на пустом списке", () => {
    const { container } = render(
      <Completion items={[]} active={0} onPick={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  describe("прокрутка активной строки", () => {
    // jsdom не реализует scrollIntoView — подставляем шпион на весь прототип,
    // чтобы поймать вызов независимо от того, на каком узле он случится.
    const scrollIntoView = vi.fn();
    HTMLElement.prototype.scrollIntoView = scrollIntoView;

    afterEach(() => {
      scrollIntoView.mockClear();
    });

    it("держит новую активную строку в видимой области при смене active", () => {
      const { rerender } = render(
        <Completion items={ITEMS} active={0} onPick={vi.fn()} />,
      );
      scrollIntoView.mockClear(); // сбрасываем вызов из начального рендера

      rerender(<Completion items={ITEMS} active={1} onPick={vi.fn()} />);

      expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest" });
    });
  });
});
