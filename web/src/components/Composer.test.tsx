import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  Attachment,
  ExecutorOption,
  FileSuggestion,
  ProviderCard,
  SandboxOption,
  SlashCommand,
} from "../api/types";
import { Composer } from "./Composer";

const TWO_PROVIDERS: ProviderCard[] = [
  { name: "router", base_url: "https://x/v1", model: "m0", is_default: true },
  { name: "backup", base_url: "https://y/v1", model: "m1", is_default: false },
];

const base = {
  onSend: () => {},
  autonomy: "supervised" as const,
  onAutonomyChange: () => {},
  executors: [] as ExecutorOption[],
  onExecutorChange: () => {},
  sandboxes: [] as SandboxOption[],
  onSandboxChange: () => {},
  providers: [] as ProviderCard[],
  provider: "",
  onProviderChange: () => {},
  model: "qwen3-coder",
  models: [],
  modelsError: null,
  onModelChange: () => {},
  commands: [] as SlashCommand[],
  onFileQuery: () => Promise.resolve([] as FileSuggestion[]),
  attachments: [] as Attachment[],
  onAttach: () => {},
  onRemoveAttachment: () => {},
};

describe("селект sandbox", () => {
  const SANDBOXES: SandboxOption[] = [
    { value: "docker", available: true, is_active: true },
    { value: "local-trusted", available: true, is_active: false },
  ];

  it("показывает варианты и отдаёт выбор наружу", () => {
    const onSandboxChange = vi.fn();
    render(
      <Composer
        {...base}
        sandboxes={SANDBOXES}
        onSandboxChange={onSandboxChange}
      />,
    );
    const select = screen.getByLabelText("Sandbox");
    fireEvent.change(select, { target: { value: "local-trusted" } });
    expect(onSandboxChange).toHaveBeenCalledWith("local-trusted");
  });

  it("недоступный docker виден, но выключен, с подсказкой", () => {
    render(
      <Composer
        {...base}
        sandboxes={[
          { value: "docker", available: false, is_active: false },
          { value: "local-trusted", available: true, is_active: true },
        ]}
      />,
    );
    const option = within(screen.getByLabelText("Sandbox")).getByRole(
      "option",
      { name: "docker" },
    ) as HTMLOptionElement;
    expect(option.disabled).toBe(true);
    expect(option.title).toContain("docker");
  });

  it("пустой список — заглушка и выключенный селект", () => {
    render(<Composer {...base} sandboxes={[]} />);
    const select = screen.getByLabelText("Sandbox") as HTMLSelectElement;
    expect(select.disabled).toBe(true);
  });
});

describe("поле ввода", () => {
  it("отправляет текст и очищает поле", async () => {
    const onSend = vi.fn();
    render(<Composer {...base} onSend={onSend} />);

    const field = screen.getByRole("combobox", { name: /написать/i });
    await userEvent.type(field, "прогони тесты");
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    expect(onSend).toHaveBeenCalledWith("прогони тесты", []);
    expect(field).toHaveValue("");
  });

  it("не отправляет пустое", async () => {
    const onSend = vi.fn();
    render(<Composer {...base} onSend={onSend} />);
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
    expect(onSend).not.toHaveBeenCalled();
  });

  it("показывает автономию сырыми значениями, как в настройках", () => {
    render(<Composer {...base} />);
    const select = screen.getByLabelText("Автономия");
    expect(
      within(select).getByRole("option", { name: "supervised" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("под надзором")).not.toBeInTheDocument();
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

  it("перечисляет исполнителей по адаптерам и гасит недоступные", () => {
    render(
      <Composer
        {...base}
        executors={[
          {
            value: "native",
            kind: "native",
            adapter: null,
            available: true,
            is_active: true,
          },
          {
            value: "claude-code",
            kind: "external",
            adapter: "claude-code",
            available: true,
            is_active: false,
          },
          {
            value: "codex",
            kind: "external",
            adapter: "codex",
            available: false,
            is_active: false,
          },
        ]}
      />,
    );
    const select = screen.getByLabelText("Исполнитель");
    expect(
      within(select).getByRole("option", { name: "codex" }),
    ).toBeDisabled();
    expect(
      within(select).getByRole("option", { name: "claude-code" }),
    ).toBeEnabled();
  });

  it("переключает исполнителя и сообщает выбор наверх", async () => {
    const onExecutorChange = vi.fn();
    render(
      <Composer
        {...base}
        onSend={() => {}}
        onExecutorChange={onExecutorChange}
        executors={[
          {
            value: "native",
            kind: "native",
            adapter: null,
            available: true,
            is_active: true,
          },
          {
            value: "claude-code",
            kind: "external",
            adapter: "claude-code",
            available: true,
            is_active: false,
          },
        ]}
      />,
    );

    await userEvent.selectOptions(
      screen.getByLabelText("Исполнитель"),
      "claude-code",
    );

    expect(onExecutorChange).toHaveBeenCalledWith("claude-code");
  });

  it("гасит выбор исполнителя, пока список не пришёл", () => {
    render(<Composer {...base} onSend={() => {}} executors={[]} />);

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
    render(
      <Composer
        {...base}
        onSend={() => {}}
        executors={[
          {
            value: "claude-code",
            kind: "external",
            adapter: "claude-code",
            available: true,
            is_active: true,
          },
        ]}
      />,
    );

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

  it("показывает подсказки команд и вставляет выбранную", async () => {
    render(
      <Composer
        {...base}
        commands={[{ name: "help", usage: "/help", help: "показать команды" }]}
      />,
    );
    const field = screen.getByLabelText("Написать Сварогу");

    await userEvent.type(field, "/he");

    expect(screen.getByText("показать команды")).toBeInTheDocument();
    await userEvent.keyboard("{Enter}");
    expect(field).toHaveValue("/help ");
  });

  it("подсказки команд ищутся без учёта регистра, как в chat_completion.py", async () => {
    // chat_completion.py:86 (сервер CLI) сравнивает без учёта регистра —
    // "/HE" там находит "/help". Веб обязан вести себя так же.
    render(
      <Composer
        {...base}
        commands={[{ name: "help", usage: "/help", help: "показать команды" }]}
      />,
    );
    const field = screen.getByLabelText("Написать Сварогу");

    await userEvent.type(field, "/HE");

    expect(screen.getByText("показать команды")).toBeInTheDocument();
  });

  it("Enter при открытом меню не отправляет сообщение", async () => {
    const onSend = vi.fn();
    render(
      <Composer
        {...base}
        onSend={onSend}
        commands={[{ name: "help", usage: "/help", help: "h" }]}
      />,
    );

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "/he{Enter}",
    );

    expect(onSend).not.toHaveBeenCalled();
  });

  it("Escape закрывает меню, а следующий Enter отправляет", async () => {
    const onSend = vi.fn();
    render(
      <Composer
        {...base}
        onSend={onSend}
        commands={[{ name: "help", usage: "/help", help: "h" }]}
      />,
    );

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "/he{Escape}{Enter}",
    );

    expect(onSend).toHaveBeenCalledWith("/he", []);
  });

  it("вставка картинки из буфера прикрепляет её", async () => {
    const onAttach = vi.fn();
    render(<Composer {...base} onAttach={onAttach} />);
    const field = screen.getByLabelText("Написать Сварогу");
    const file = new File([new Uint8Array([1])], "скрин.png", {
      type: "image/png",
    });

    fireEvent.paste(field, { clipboardData: { files: [file], items: [] } });

    expect(onAttach).toHaveBeenCalledWith(file);
  });

  it("прикрепляет файл, перетащенный на поле", () => {
    const onAttach = vi.fn();
    render(<Composer {...base} onAttach={onAttach} />);
    const field = screen.getByLabelText("Написать Сварогу");
    const file = new File([new Uint8Array([1])], "план.txt", {
      type: "text/plain",
    });

    fireEvent.drop(field, { dataTransfer: { files: [file] } });

    expect(onAttach).toHaveBeenCalledWith(file);
  });

  it("прикрепляет файл через кнопку-скрепку", async () => {
    const onAttach = vi.fn();
    render(<Composer {...base} onAttach={onAttach} />);
    const file = new File([new Uint8Array([1])], "лог.txt", {
      type: "text/plain",
    });

    // Кнопка видна и подписана; сам выбор файла идёт через связанный с ней
    // скрытый input — у него нарочно нет своего aria-label (см. комментарий
    // в Composer.tsx), поэтому находим его по типу.
    expect(screen.getByLabelText("Прикрепить файл")).toBeInTheDocument();
    const input = document.querySelector('input[type="file"]');
    if (input === null) throw new Error("input[type=file] не найден");
    await userEvent.upload(input as HTMLInputElement, file);

    expect(onAttach).toHaveBeenCalledWith(file);
  });

  it("показывает вложения над полем ввода", () => {
    render(
      <Composer
        {...base}
        attachments={[
          {
            path: ".attachments/a.png",
            name: "a.png",
            size_bytes: 1,
            mime: "image/png",
            too_large_for_vision: false,
          },
        ]}
      />,
    );
    expect(screen.getByText("a.png")).toBeInTheDocument();
  });

  it("подсказывает файлы после @ и вставляет выбранный", async () => {
    const onFileQuery = vi
      .fn()
      .mockResolvedValue([{ path: "src/app.py", label: "src/app.py" }]);
    render(<Composer {...base} onFileQuery={onFileQuery} />);
    const field = screen.getByLabelText("Написать Сварогу");

    await userEvent.type(field, "глянь @sr");

    expect(await screen.findByText("src/app.py")).toBeInTheDocument();
    await userEvent.keyboard("{Enter}");
    expect(field).toHaveValue("глянь @src/app.py ");
    expect(onFileQuery).toHaveBeenCalledWith("sr");
  });

  it("дебaунсит запрос файловых подсказок, а не шлёт его на каждую букву", async () => {
    const onFileQuery = vi.fn().mockResolvedValue([]);
    render(<Composer {...base} onFileQuery={onFileQuery} />);
    const field = screen.getByLabelText("Написать Сварогу");

    fireEvent.change(field, { target: { value: "@a" } });
    expect(onFileQuery).not.toHaveBeenCalled();

    // Вторая буква приходит раньше, чем истёк дебаунс первой — предыдущий
    // таймер обязан быть снят, а не выстрелить запросом на "a".
    await new Promise((r) => setTimeout(r, 60));
    fireEvent.change(field, { target: { value: "@ab" } });
    expect(onFileQuery).not.toHaveBeenCalled();

    await waitFor(() => expect(onFileQuery).toHaveBeenCalledTimes(1));
    expect(onFileQuery).toHaveBeenCalledWith("ab");
  });

  it("поле ввода — комбобокс со ссылкой на список подсказок, а не голый textbox", async () => {
    const onFileQuery = vi
      .fn()
      .mockResolvedValue([{ path: "src/app.py", label: "src/app.py" }]);
    render(<Composer {...base} onFileQuery={onFileQuery} />);
    const field = screen.getByLabelText("Написать Сварогу");

    expect(field).toHaveAttribute("role", "combobox");
    expect(field).toHaveAttribute("aria-autocomplete", "list");
    expect(field).toHaveAttribute("aria-expanded", "false");
    expect(field).not.toHaveAttribute("aria-controls");

    await userEvent.type(field, "@sr");
    const list = await screen.findByRole("listbox");

    expect(field).toHaveAttribute("aria-expanded", "true");
    expect(field).toHaveAttribute("aria-controls", list.id);
  });

  it("клик по подсказке не уводит фокус с поля", async () => {
    render(
      <Composer
        {...base}
        commands={[{ name: "help", usage: "/help", help: "показать команды" }]}
      />,
    );
    const field = screen.getByLabelText("Написать Сварогу");
    await userEvent.type(field, "/he");
    const suggestion = screen.getByText("показать команды");

    await userEvent.click(suggestion);

    expect(field).toHaveValue("/help ");
    expect(document.activeElement).toBe(field);
  });
});

describe("блокировка отправки во время загрузки", () => {
  it("не отправляет по Enter и гасит кнопку отправки, пока uploading=true", async () => {
    const onSend = vi.fn();
    render(<Composer {...base} onSend={onSend} uploading />);
    const field = screen.getByLabelText("Написать Сварогу");
    const sendButton = screen.getByRole("button", { name: "Отправить" });

    expect(sendButton).toBeDisabled();

    await userEvent.type(field, "текст{Enter}");
    expect(onSend).not.toHaveBeenCalled();

    await userEvent.click(sendButton);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("отправляет как обычно, когда uploading=false", async () => {
    const onSend = vi.fn();
    render(<Composer {...base} onSend={onSend} uploading={false} />);

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "текст{Enter}",
    );

    expect(onSend).toHaveBeenCalledWith("текст", []);
  });
});

describe("клавиатура", () => {
  it("отправляет по Enter", async () => {
    const onSend = vi.fn();
    render(<Composer {...base} onSend={onSend} />);

    const field = screen.getByRole("combobox", { name: /написать/i });
    await userEvent.type(field, "прогони тесты{Enter}");

    expect(onSend).toHaveBeenCalledWith("прогони тесты", []);
    expect(field).toHaveValue("");
  });

  it("Shift+Enter переносит строку, а не отправляет", async () => {
    const onSend = vi.fn();
    render(<Composer {...base} onSend={onSend} />);

    const field = screen.getByRole("combobox", { name: /написать/i });
    await userEvent.type(field, "первая{Shift>}{Enter}{/Shift}вторая");

    expect(onSend).not.toHaveBeenCalled();
    expect(field).toHaveValue("первая\nвторая");
  });
});
