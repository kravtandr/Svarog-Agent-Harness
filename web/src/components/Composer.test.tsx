import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Composer } from "./Composer";

const base = {
  autonomy: "supervised" as const,
  onAutonomyChange: () => {},
  executor: "native" as const,
  onExecutorChange: () => {},
  providers: [],
  provider: "",
  onProviderChange: () => {},
  model: "qwen3-coder",
  models: [],
  modelsError: null,
  onModelChange: () => {},
};

describe("поле ввода", () => {
  it("отправляет текст и очищает поле", async () => {
    const onSend = vi.fn();
    render(<Composer {...base} onSend={onSend} />);

    const field = screen.getByRole("textbox", { name: /написать/i });
    await userEvent.type(field, "прогони тесты");
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    expect(onSend).toHaveBeenCalledWith("прогони тесты");
    expect(field).toHaveValue("");
  });

  it("не отправляет пустое", async () => {
    const onSend = vi.fn();
    render(<Composer {...base} onSend={onSend} />);
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
    expect(onSend).not.toHaveBeenCalled();
  });

  it("переключает автономию и сообщает выбор наверх", async () => {
    const onAutonomyChange = vi.fn();
    render(
      <Composer
        {...base}
        onAutonomyChange={onAutonomyChange}
        onSend={() => {}}
      />,
    );

    const select = screen.getByRole("combobox", { name: /автономия/i });
    expect(select).toHaveValue("supervised");

    await userEvent.selectOptions(select, "yolo");
    expect(onAutonomyChange).toHaveBeenCalledWith("yolo");
  });

  it("переключает исполнителя", async () => {
    const onExecutorChange = vi.fn();
    render(
      <Composer
        {...base}
        onSend={() => {}}
        onExecutorChange={onExecutorChange}
      />,
    );

    await userEvent.selectOptions(
      screen.getByLabelText("Исполнитель"),
      "внешний агент",
    );

    expect(onExecutorChange).toHaveBeenCalledWith("external");
  });

  it("гасит выбор модели у внешнего агента", () => {
    render(<Composer {...base} onSend={() => {}} executor="external" />);

    const button = screen.getByLabelText("Выбрать модель");
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute(
      "title",
      expect.stringContaining("своему провайдеру"),
    );
  });

  it("показывает модель из конфига, а не выдуманную", () => {
    render(
      <Composer
        {...base}
        onSend={() => {}}
        model="deepseek/deepseek-v4-flash"
      />,
    );
    expect(screen.getByText("deepseek/deepseek-v4-flash")).toBeInTheDocument();
  });

  it("держит место под микрофон выключенной кнопкой", () => {
    render(<Composer {...base} onSend={() => {}} />);
    const mic = screen.getByRole("button", { name: /голосовой ввод/i });
    expect(mic).toBeDisabled();
    expect(mic).toHaveAccessibleDescription(/появится позже/i);
  });
});

describe("клавиатура", () => {
  it("отправляет по Enter", async () => {
    const onSend = vi.fn();
    render(<Composer {...base} onSend={onSend} />);

    const field = screen.getByRole("textbox", { name: /написать/i });
    await userEvent.type(field, "прогони тесты{Enter}");

    expect(onSend).toHaveBeenCalledWith("прогони тесты");
    expect(field).toHaveValue("");
  });

  it("Shift+Enter переносит строку, а не отправляет", async () => {
    const onSend = vi.fn();
    render(<Composer {...base} onSend={onSend} />);

    const field = screen.getByRole("textbox", { name: /написать/i });
    await userEvent.type(field, "первая{Shift>}{Enter}{/Shift}вторая");

    expect(onSend).not.toHaveBeenCalled();
    expect(field).toHaveValue("первая\nвторая");
  });
});
