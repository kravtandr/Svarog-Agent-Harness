import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ModelPicker } from "./ModelPicker";
import type { ModelCard } from "../api/types";

const MODELS: ModelCard[] = [
  {
    id: "deepseek/deepseek-v4-flash",
    name: "DeepSeek V4 Flash",
    context_length: 163840,
    input_usd_per_mtok: 0.5,
    output_usd_per_mtok: 1.5,
  },
  {
    id: "anthropic/claude-opus",
    name: "Claude Opus",
    context_length: 200000,
    input_usd_per_mtok: 15,
    output_usd_per_mtok: 75,
  },
];

describe("ModelPicker", () => {
  it("фильтрует по id и по имени", async () => {
    render(
      <ModelPicker
        models={MODELS}
        current="deepseek/deepseek-v4-flash"
        error={null}
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    await userEvent.type(screen.getByLabelText("Поиск модели"), "opus");

    expect(screen.getByText("Claude Opus")).toBeInTheDocument();
    expect(screen.queryByText("DeepSeek V4 Flash")).not.toBeInTheDocument();
  });

  it("возвращает выбранную модель", async () => {
    const onPick = vi.fn();
    render(
      <ModelPicker
        models={MODELS}
        current=""
        error={null}
        onPick={onPick}
        onClose={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByText("Claude Opus"));

    expect(onPick).toHaveBeenCalledWith("anthropic/claude-opus");
  });

  it("показывает причину, когда каталог не пришёл", () => {
    render(
      <ModelPicker
        models={[]}
        current=""
        error="провайдер ответил 401"
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText(/401/)).toBeInTheDocument();
  });

  it("закрывается по Escape", async () => {
    const onClose = vi.fn();
    render(
      <ModelPicker
        models={MODELS}
        current=""
        error={null}
        onPick={vi.fn()}
        onClose={onClose}
      />,
    );

    await userEvent.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalled();
  });

  it("рисует карточку с одними null-полями без null/undefined в разметке", () => {
    const bare: ModelCard = {
      id: "local/bare-model",
      name: null,
      context_length: null,
      input_usd_per_mtok: null,
      output_usd_per_mtok: null,
    };
    render(
      <ModelPicker
        models={[bare]}
        current=""
        error={null}
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    // Имени нет — показываем id как есть.
    expect(screen.getByText("local/bare-model")).toBeInTheDocument();
    expect(screen.queryByText(/null/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/undefined/i)).not.toBeInTheDocument();
  });

  it("закрывается по Escape, даже если фокус ушёл за пределы панели", async () => {
    const onClose = vi.fn();
    render(
      <>
        <button type="button">снаружи</button>
        <ModelPicker
          models={MODELS}
          current=""
          error={null}
          onPick={vi.fn()}
          onClose={onClose}
        />
      </>,
    );

    // Табом (или чем угодно) фокус мог уйти за пределы панели — например,
    // на соседний элемент композера, который появится в следующей задаче.
    screen.getByText("снаружи").focus();
    expect(screen.getByText("снаружи")).toHaveFocus();

    await userEvent.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalled();
  });
});
