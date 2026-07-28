import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ProviderCard } from "../api/types";
import { Composer } from "./Composer";

const TWO_PROVIDERS: ProviderCard[] = [
  { name: "router", base_url: "https://x/v1", model: "m0", is_default: true },
  { name: "backup", base_url: "https://y/v1", model: "m1", is_default: false },
];

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

  it("гасит выбор исполнителя, пока /config не ответил", () => {
    render(<Composer {...base} onSend={() => {}} executor={null} />);

    const select = screen.getByLabelText("Исполнитель");
    expect(select).toBeDisabled();
    expect(select).toHaveValue("");
  });

  it("показывает провайдера, когда их больше одного", () => {
    render(
      <Composer
        {...base}
        onSend={() => {}}
        providers={TWO_PROVIDERS}
        provider="router"
      />,
    );

    const select = screen.getByLabelText("Провайдер");
    expect(select).toHaveValue("router");
    expect(screen.getByRole("option", { name: "backup" })).toBeInTheDocument();
  });

  it("переключает провайдера", async () => {
    const onProviderChange = vi.fn();
    render(
      <Composer
        {...base}
        onSend={() => {}}
        providers={TWO_PROVIDERS}
        provider="router"
        onProviderChange={onProviderChange}
      />,
    );

    await userEvent.selectOptions(screen.getByLabelText("Провайдер"), "backup");

    expect(onProviderChange).toHaveBeenCalledWith("backup");
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
