import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Composer } from "./Composer";

const props = {
  autonomy: "supervised" as const,
  onAutonomyChange: () => {},
  executor: "нативный цикл",
  model: "qwen3-coder",
};

describe("поле ввода", () => {
  it("отправляет текст и очищает поле", async () => {
    const onSend = vi.fn();
    render(<Composer {...props} onSend={onSend} />);

    const field = screen.getByRole("textbox", { name: /написать/i });
    await userEvent.type(field, "прогони тесты");
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    expect(onSend).toHaveBeenCalledWith("прогони тесты");
    expect(field).toHaveValue("");
  });

  it("не отправляет пустое", async () => {
    const onSend = vi.fn();
    render(<Composer {...props} onSend={onSend} />);
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
    expect(onSend).not.toHaveBeenCalled();
  });

  it("переключает автономию и сообщает выбор наверх", async () => {
    const onAutonomyChange = vi.fn();
    render(
      <Composer
        {...props}
        onAutonomyChange={onAutonomyChange}
        onSend={() => {}}
      />,
    );

    const select = screen.getByRole("combobox", { name: /автономия/i });
    expect(select).toHaveValue("supervised");

    await userEvent.selectOptions(select, "yolo");
    expect(onAutonomyChange).toHaveBeenCalledWith("yolo");
  });

  it("показывает исполнителя и модель, но не даёт их менять здесь", () => {
    render(<Composer {...props} onSend={() => {}} />);

    const fixed = screen.getAllByTitle(/меняется в настройках/i);
    expect(fixed.map((node) => node.textContent)).toEqual([
      "нативный цикл",
      "qwen3-coder",
    ]);
    // Не кнопка и не поле: менять исполнителя из ленты нельзя — это конфиг.
    expect(
      screen.queryByRole("button", { name: /нативный цикл/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: /исполнитель/i }),
    ).not.toBeInTheDocument();
  });

  it("держит место под микрофон выключенной кнопкой", () => {
    render(<Composer {...props} onSend={() => {}} />);
    const mic = screen.getByRole("button", { name: /голосовой ввод/i });
    expect(mic).toBeDisabled();
    expect(mic).toHaveAccessibleDescription(/появится позже/i);
  });
});

describe("клавиатура", () => {
  it("отправляет по Enter", async () => {
    const onSend = vi.fn();
    render(<Composer {...props} onSend={onSend} />);

    const field = screen.getByRole("textbox", { name: /написать/i });
    await userEvent.type(field, "прогони тесты{Enter}");

    expect(onSend).toHaveBeenCalledWith("прогони тесты");
    expect(field).toHaveValue("");
  });

  it("Shift+Enter переносит строку, а не отправляет", async () => {
    const onSend = vi.fn();
    render(<Composer {...props} onSend={onSend} />);

    const field = screen.getByRole("textbox", { name: /написать/i });
    await userEvent.type(field, "первая{Shift>}{Enter}{/Shift}вторая");

    expect(onSend).not.toHaveBeenCalled();
    expect(field).toHaveValue("первая\nвторая");
  });
});
