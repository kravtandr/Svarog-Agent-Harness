import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Attachments } from "./Attachments";
import type { Attachment } from "../api/types";

const IMAGE: Attachment = {
  path: ".attachments/ab_скрин.png",
  name: "скрин.png",
  size_bytes: 2048,
  mime: "image/png",
  too_large_for_vision: false,
};
const DOC: Attachment = {
  path: ".attachments/cd_отчёт.pdf",
  name: "отчёт.pdf",
  size_bytes: 100,
  mime: null,
  too_large_for_vision: false,
};

describe("Attachments", () => {
  it("показывает исходное имя, а не имя на диске", () => {
    render(<Attachments items={[IMAGE]} onRemove={vi.fn()} />);
    expect(screen.getByText("скрин.png")).toBeInTheDocument();
    expect(screen.queryByText(/ab_/)).not.toBeInTheDocument();
  });

  it("убирает вложение крестиком", async () => {
    const onRemove = vi.fn();
    render(<Attachments items={[DOC]} onRemove={onRemove} />);
    await userEvent.click(screen.getByLabelText("Убрать отчёт.pdf"));
    expect(onRemove).toHaveBeenCalledWith(".attachments/cd_отчёт.pdf");
  });

  it("предупреждает, что картинка слишком велика для модели", () => {
    render(
      <Attachments
        items={[{ ...IMAGE, too_large_for_vision: true }]}
        onRemove={vi.fn()}
      />,
    );
    expect(screen.getByText(/модель не увидит/i)).toBeInTheDocument();
  });

  it("ничего не рисует без вложений", () => {
    const { container } = render(<Attachments items={[]} onRemove={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });
});
