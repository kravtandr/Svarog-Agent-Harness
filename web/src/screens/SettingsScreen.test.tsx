import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Api } from "../api/client";
import type { ConfigView } from "../api/types";
import { fakeApi as baseApi } from "../test/fakeApi";
import { SettingsScreen } from "./SettingsScreen";

const config: ConfigView = {
  path: "/agent-home/svarog.yaml",
  sections: [
    {
      key: "policies",
      title: "Политики и автономия",
      fields: [
        {
          path: "runtime.autonomy",
          label: "Уровень автономии",
          help: "Как поступать с действиями среднего риска.",
          kind: "enum",
          value: "yolo",
          choices: ["supervised", "auto", "yolo"],
          minimum: null,
          maximum: null,
        },
        {
          path: "runtime.max_iterations",
          label: "Максимум шагов в одном запуске",
          help: "",
          kind: "int",
          value: 50,
          choices: [],
          minimum: 0,
          maximum: null,
        },
        {
          path: "git.require_approval_for_push",
          label: "Спрашивать перед push",
          help: "",
          kind: "bool",
          value: true,
          choices: [],
          minimum: null,
          maximum: null,
        },
      ],
    },
  ],
};

const fakeApi = (over: Partial<Api> = {}): Api =>
  baseApi({
    config: vi.fn().mockResolvedValue(config),
    previewConfig: vi.fn().mockResolvedValue({
      path: config.path,
      changes: 2,
      lines: [
        { kind: "same", text: "runtime:" },
        { kind: "del", text: "  autonomy: yolo" },
        { kind: "add", text: "  autonomy: supervised" },
      ],
      restart_required: false,
    }),
    saveConfig: vi.fn().mockResolvedValue({
      path: config.path,
      changes: 0,
      lines: [],
      restart_required: false,
    }),
    secrets: vi.fn().mockResolvedValue([
      { name: "PROVIDER_API_KEY", present: true },
      { name: "GITHUB_TOKEN", present: false },
    ]),
    ...over,
  });

describe("экран настроек", () => {
  it("строит форму из ответа сервера", async () => {
    render(<SettingsScreen api={fakeApi()} />);

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Политики и автономия" }),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("combobox", { name: /уровень автономии/i }),
    ).toHaveValue("yolo");
    expect(
      screen.getByRole("spinbutton", { name: /максимум шагов/i }),
    ).toHaveValue(50);
    expect(
      screen.getByRole("checkbox", { name: /спрашивать перед push/i }),
    ).toBeChecked();
  });

  it("показывает дифф файла после правки и не сохраняет сам", async () => {
    const api = fakeApi();
    render(<SettingsScreen api={api} />);
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Политики и автономия" }),
      ).toBeInTheDocument(),
    );

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /уровень автономии/i }),
      "supervised",
    );

    await waitFor(() =>
      expect(api.previewConfig).toHaveBeenCalledWith({
        "runtime.autonomy": "supervised",
      }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Показать дифф" }),
    );
    const pane = screen.getByTestId("diffbar");
    await waitFor(() => expect(pane).toHaveTextContent("autonomy: supervised"));
    // Добавленная строка помечена как добавленная, а не просто выведена.
    const added = pane.querySelectorAll(".diffpane__line--add");
    expect([...added].map((node) => node.textContent)).toEqual([
      "+  autonomy: supervised",
    ]);
    expect(api.saveConfig).not.toHaveBeenCalled();
  });

  it("сохраняет только по нажатию и сообщает число изменений", async () => {
    const api = fakeApi();
    render(<SettingsScreen api={api} />);
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Политики и автономия" }),
      ).toBeInTheDocument(),
    );

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /уровень автономии/i }),
      "supervised",
    );
    await waitFor(() =>
      expect(screen.getByText(/2 изменения/)).toBeInTheDocument(),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Показать дифф" }),
    );

    await userEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    expect(api.saveConfig).toHaveBeenCalledWith({
      "runtime.autonomy": "supervised",
    });
  });

  it("при нуле изменений полосы сохранения нет вовсе", async () => {
    const api = baseApi({ config: vi.fn().mockResolvedValue(config) });
    render(<SettingsScreen api={api} />);
    await waitFor(() =>
      expect(screen.getByLabelText("Уровень автономии")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("diffbar")).not.toBeInTheDocument();
  });

  it("правка поля поднимает полосу с числом изменений", async () => {
    const api = baseApi({
      config: vi.fn().mockResolvedValue(config),
      previewConfig: vi.fn().mockResolvedValue({
        path: "/agent-home/svarog.yaml",
        lines: [{ kind: "add", text: "  autonomy: supervised" }],
        changes: 1,
        restart_required: false,
      }),
    });
    render(<SettingsScreen api={api} />);
    await waitFor(() =>
      expect(screen.getByLabelText("Уровень автономии")).toBeInTheDocument(),
    );
    await userEvent.selectOptions(
      screen.getByLabelText("Уровень автономии"),
      "supervised",
    );
    await waitFor(() =>
      expect(screen.getByTestId("diffbar")).toHaveTextContent("1 изменение"),
    );
  });

  it("дифф раскрывается по кнопке, а не занимает место постоянно", async () => {
    const api = baseApi({
      config: vi.fn().mockResolvedValue(config),
      previewConfig: vi.fn().mockResolvedValue({
        path: "/agent-home/svarog.yaml",
        lines: [{ kind: "add", text: "  autonomy: supervised" }],
        changes: 1,
        restart_required: false,
      }),
    });
    render(<SettingsScreen api={api} />);
    await waitFor(() =>
      expect(screen.getByLabelText("Уровень автономии")).toBeInTheDocument(),
    );
    await userEvent.selectOptions(
      screen.getByLabelText("Уровень автономии"),
      "supervised",
    );
    await waitFor(() =>
      expect(screen.getByTestId("diffbar")).toBeInTheDocument(),
    );
    expect(screen.queryByText(/autonomy: supervised/)).not.toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Показать дифф" }),
    );
    expect(screen.getByText(/autonomy: supervised/)).toBeInTheDocument();
  });

  it("показывает отказ схемы на месте, а не общим сообщением", async () => {
    const { ApiError } = await import("../api/client");
    const api = fakeApi({
      previewConfig: vi
        .fn()
        .mockRejectedValue(
          new ApiError(422, "max_iterations: должно быть > 0"),
        ),
    });
    render(<SettingsScreen api={api} />);
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Политики и автономия" }),
      ).toBeInTheDocument(),
    );

    const steps = screen.getByRole("spinbutton", { name: /максимум шагов/i });
    await userEvent.clear(steps);
    await userEvent.type(steps, "0");

    await waitFor(() =>
      expect(screen.getByText(/должно быть > 0/)).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Сохранить" })).toBeDisabled();
  });

  it("сообщает, что правка вступит в силу после текущих запусков", async () => {
    const api = fakeApi({
      saveConfig: vi.fn().mockResolvedValue({
        path: config.path,
        changes: 0,
        lines: [],
        restart_required: true,
      }),
    });
    render(<SettingsScreen api={api} />);
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Политики и автономия" }),
      ).toBeInTheDocument(),
    );

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /уровень автономии/i }),
      "supervised",
    );
    await waitFor(() =>
      expect(screen.getByText(/2 изменения/)).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() =>
      expect(
        screen.getByText(/вступит в силу.*текущ.*запуск/i),
      ).toBeInTheDocument(),
    );
  });

  it("не показывает эту заметку, когда перезапуск не нужен", async () => {
    const api = fakeApi();
    render(<SettingsScreen api={api} />);
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Политики и автономия" }),
      ).toBeInTheDocument(),
    );

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /уровень автономии/i }),
      "supervised",
    );
    await waitFor(() =>
      expect(screen.getByText(/2 изменения/)).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(api.saveConfig).toHaveBeenCalled());
    expect(
      screen.queryByText(/вступит в силу.*текущ.*запуск/i),
    ).not.toBeInTheDocument();
  });

  it("переключает провайдера по умолчанию из списка", async () => {
    const api = fakeApi({
      providers: vi.fn().mockResolvedValue([
        {
          name: "local",
          base_url: "https://x/v1",
          model: "m",
          is_default: true,
        },
        {
          name: "LiteLLM",
          base_url: "https://lite/v1",
          model: "qwen3",
          is_default: false,
        },
      ]),
    });
    render(<SettingsScreen api={api} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Провайдеры" }),
    );

    // Кнопка есть только у не-дефолтного провайдера.
    await userEvent.click(
      await screen.findByRole("button", { name: "По умолчанию" }),
    );

    await waitFor(() =>
      expect(api.executorDefaults).toHaveBeenCalledWith({
        executor: "native",
        provider: "LiteLLM",
      }),
    );
    expect(
      screen.getByText(/Теперь по умолчанию — «LiteLLM»/),
    ).toBeInTheDocument();
  });

  it("разворачивает каталог сохранённого провайдера и подставляет модель в форму", async () => {
    const api = fakeApi({
      providers: vi.fn().mockResolvedValue([
        {
          name: "local",
          base_url: "https://x/v1",
          model: "m",
          is_default: true,
        },
      ]),
      providerModels: vi.fn().mockResolvedValue([
        {
          id: "qwen3-32b",
          name: "Qwen3 32B",
          context_length: 32768,
          input_usd_per_mtok: null,
          output_usd_per_mtok: null,
        },
      ]),
    });
    render(<SettingsScreen api={api} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Провайдеры" }),
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "Модели" }),
    );
    await userEvent.click(await screen.findByText("Qwen3 32B"));

    expect(api.providerModels).toHaveBeenCalledWith("local");
    expect(screen.getByLabelText("Имя")).toHaveValue("local");
    expect(screen.getByLabelText("Base URL (с /v1)")).toHaveValue(
      "https://x/v1",
    );
    expect(screen.getByLabelText("Модель по умолчанию")).toHaveValue(
      "qwen3-32b",
    );

    // Сохранение сбрасывает кэш каталогов — секция обязана свернуться,
    // иначе она застряла бы на «Загружаем…» без повторного запроса.
    await userEvent.click(
      screen.getByRole("button", { name: "Сохранить провайдера" }),
    );
    await waitFor(() => expect(api.addProvider).toHaveBeenCalled());
    expect(screen.queryByText(/Загружаем каталог/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Модели" })).toBeInTheDocument();
  });

  const twoProviders = () =>
    vi.fn().mockResolvedValue([
      {
        name: "local",
        base_url: "https://openrouter.ai/api/v1",
        model: "deepseek/x",
        is_default: true,
      },
      {
        name: "groq",
        base_url: "https://api.groq.com/openai/v1",
        model: "ll",
        is_default: false,
      },
    ]);

  it("проверяет доступность провайдера из строки", async () => {
    const api = fakeApi({
      providers: twoProviders(),
      providerCheck: vi
        .fn()
        .mockResolvedValueOnce({ ok: true, models_count: 317, error: null })
        .mockResolvedValueOnce({
          ok: false,
          models_count: null,
          error: "провайдер ответил 401",
        }),
    });
    render(<SettingsScreen api={api} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Провайдеры" }),
    );

    // «Ещё» — общий на все карточки: раскрыт только один провайдер за раз,
    // так что проверяем по очереди, а не два «Проверить» разом.
    await userEvent.click(
      await screen.findByRole("button", { name: "Ещё local" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Проверить" }));
    expect(
      await screen.findByText(/доступен · 317 моделей/),
    ).toBeInTheDocument();
    expect(api.providerCheck).toHaveBeenCalledWith("local");

    await userEvent.click(screen.getByRole("button", { name: "Ещё groq" }));
    await userEvent.click(screen.getByRole("button", { name: "Проверить" }));
    expect(
      await screen.findByText(/провайдер ответил 401/),
    ).toBeInTheDocument();
  });

  it("переименовывает провайдера через инлайн-поле", async () => {
    const api = fakeApi({ providers: twoProviders() });
    render(<SettingsScreen api={api} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Провайдеры" }),
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "Ещё local" }),
    );
    const renames = await screen.findAllByRole("button", {
      name: "Переименовать",
    });
    await userEvent.click(renames[0]);
    const field = screen.getByRole("textbox", { name: "Новое имя local" });
    await userEvent.clear(field);
    await userEvent.type(field, "openrouter");
    await userEvent.click(screen.getByRole("button", { name: "OK" }));

    await waitFor(() =>
      expect(api.providerRename).toHaveBeenCalledWith("local", "openrouter"),
    );
    // Список перечитан после успеха.
    expect(api.providers).toHaveBeenCalledTimes(2);
  });

  it("отправляет инлайн-переименование провайдера по Enter", async () => {
    const api = fakeApi({ providers: twoProviders() });
    render(<SettingsScreen api={api} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Провайдеры" }),
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "Ещё local" }),
    );
    const renames = await screen.findAllByRole("button", {
      name: "Переименовать",
    });
    await userEvent.click(renames[0]);
    const field = screen.getByRole("textbox", { name: "Новое имя local" });
    await userEvent.clear(field);
    await userEvent.type(field, "openrouter{Enter}");

    await waitFor(() =>
      expect(api.providerRename).toHaveBeenCalledWith("local", "openrouter"),
    );
  });

  it("удаляет провайдера после повторного клика, дефолтный — без кнопки", async () => {
    const api = fakeApi({ providers: twoProviders() });
    render(<SettingsScreen api={api} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Провайдеры" }),
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "Ещё groq" }),
    );
    // «Удалить» есть только у не-дефолтного.
    const remove = await screen.findByRole("button", { name: "Удалить" });
    await userEvent.click(remove);
    expect(api.providerRemove).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "Точно удалить?" }),
    );
    await waitFor(() =>
      expect(api.providerRemove).toHaveBeenCalledWith("groq"),
    );
  });

  it("«Изменить» правит провайдера в его же карточке, не трогая форму добавления", async () => {
    const api = baseApi({
      config: vi.fn().mockResolvedValue(config),
      providers: vi.fn().mockResolvedValue([
        {
          name: "groq",
          base_url: "https://api.groq.com/openai/v1",
          model: "llama-3.3-70b",
          is_default: false,
        },
      ]),
    });
    render(<SettingsScreen api={api} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Провайдеры" }),
    );
    await waitFor(() => expect(screen.getByText("groq")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Ещё groq" }));
    await userEvent.click(screen.getByRole("button", { name: "Изменить" }));

    // Форма добавления осталась пустой — контекст не уехал вниз экрана.
    expect(screen.getByLabelText("Имя")).toHaveValue("");
    expect(screen.getByLabelText("Модель groq")).toHaveValue("llama-3.3-70b");

    await userEvent.clear(screen.getByLabelText("Модель groq"));
    await userEvent.type(screen.getByLabelText("Модель groq"), "llama-3.1-8b");
    await userEvent.click(
      screen.getByRole("button", { name: "Сохранить groq" }),
    );
    await waitFor(() =>
      expect(api.addProvider).toHaveBeenCalledWith({
        name: "groq",
        base_url: "https://api.groq.com/openai/v1",
        model: "llama-3.1-8b",
      }),
    );
  });

  it("редкие действия провайдера спрятаны за «Ещё»", async () => {
    const api = baseApi({
      config: vi.fn().mockResolvedValue(config),
      providers: vi.fn().mockResolvedValue([
        {
          name: "groq",
          base_url: "https://api.groq.com/openai/v1",
          model: "llama-3.3-70b",
          is_default: false,
        },
      ]),
    });
    render(<SettingsScreen api={api} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Провайдеры" }),
    );
    await waitFor(() => expect(screen.getByText("groq")).toBeInTheDocument());

    expect(
      screen.queryByRole("button", { name: "Переименовать" }),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Ещё groq" }));
    expect(
      screen.getByRole("button", { name: "Переименовать" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Проверить" }),
    ).toBeInTheDocument();
  });

  it("сообщает про restart_required при сохранении провайдера", async () => {
    const api = fakeApi({
      addProvider: vi.fn().mockResolvedValue({
        path: "",
        lines: [],
        changes: 1,
        restart_required: true,
      }),
    });
    render(<SettingsScreen api={api} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Провайдеры" }),
    );

    await userEvent.type(screen.getByLabelText("Имя"), "groq");
    await userEvent.type(
      screen.getByLabelText("Base URL (с /v1)"),
      "https://api.groq.com/openai/v1",
    );
    await userEvent.type(screen.getByLabelText("Модель по умолчанию"), "ll");
    await userEvent.click(
      screen.getByRole("button", { name: "Сохранить провайдера" }),
    );

    expect(
      await screen.findByText(/вступит в силу.*текущ.*запуск/i),
    ).toBeInTheDocument();
  });

  it("сканирует /models по данным формы и честно показывает ошибку", async () => {
    const { ApiError } = await import("../api/client");
    const api = fakeApi({
      scanModels: vi
        .fn()
        .mockResolvedValueOnce([
          {
            id: "lite/qwen",
            name: null,
            context_length: null,
            input_usd_per_mtok: null,
            output_usd_per_mtok: null,
          },
        ])
        .mockRejectedValueOnce(new ApiError(502, "провайдер ответил 401")),
    });
    render(<SettingsScreen api={api} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Провайдеры" }),
    );

    await userEvent.type(
      screen.getByLabelText("Base URL (с /v1)"),
      "https://lite/v1",
    );
    await userEvent.type(screen.getByLabelText(/API-ключ/), "sk-x");
    await userEvent.click(screen.getByRole("button", { name: "Сканировать" }));

    await waitFor(() =>
      expect(api.scanModels).toHaveBeenCalledWith({
        base_url: "https://lite/v1",
        api_key: "sk-x",
      }),
    );
    // Клик по найденной модели заполняет поле.
    await userEvent.click(await screen.findByText("lite/qwen"));
    expect(screen.getByLabelText("Модель по умолчанию")).toHaveValue(
      "lite/qwen",
    );

    // Второй скан падает — ошибка на экране, поле не затирается.
    await userEvent.click(screen.getByRole("button", { name: "Сканировать" }));
    expect(
      await screen.findByText("провайдер ответил 401"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Модель по умолчанию")).toHaveValue(
      "lite/qwen",
    );
  });

  it("показывает имена секретов без значений", async () => {
    render(<SettingsScreen api={fakeApi()} />);

    await userEvent.click(
      await screen.findByRole("button", { name: /секреты/i }),
    );

    expect(await screen.findByText("PROVIDER_API_KEY")).toBeInTheDocument();
    expect(screen.getByText("задан")).toBeInTheDocument();
    expect(screen.getByText("не задан")).toBeInTheDocument();
  });

  it("исполнители: выбор провайдера открывает каталог, клик подставляет модель", async () => {
    const api = fakeApi({
      providers: twoProviders(),
      providerModels: vi.fn().mockResolvedValue([
        {
          id: "llama-3.3-70b-versatile",
          name: "Llama 3.3 70B",
          context_length: 131072,
          input_usd_per_mtok: null,
          output_usd_per_mtok: null,
        },
      ]),
    });
    render(<SettingsScreen api={api} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Исполнители" }),
    );

    await userEvent.selectOptions(
      await screen.findByRole("combobox", { name: "Провайдер native" }),
      "groq",
    );
    await userEvent.click(await screen.findByText("Llama 3.3 70B"));

    expect(api.providerModels).toHaveBeenCalledWith("groq");
    expect(screen.getByRole("textbox", { name: "Модель native" })).toHaveValue(
      "llama-3.3-70b-versatile",
    );
    // Поле остаётся редактируемым руками: каталог бывает неполным.
    await userEvent.clear(
      screen.getByRole("textbox", { name: "Модель native" }),
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: "Модель native" }),
      "своя-модель",
    );
    expect(screen.getByRole("textbox", { name: "Модель native" })).toHaveValue(
      "своя-модель",
    );
  });

  it("исполнители: недоступный каталог — текст ошибки, поле работает", async () => {
    const { ApiError } = await import("../api/client");
    const api = fakeApi({
      providers: twoProviders(),
      providerModels: vi
        .fn()
        .mockRejectedValue(new ApiError(502, "провайдер ответил 401")),
    });
    render(<SettingsScreen api={api} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Исполнители" }),
    );

    await userEvent.selectOptions(
      await screen.findByRole("combobox", { name: "Провайдер opencode" }),
      "groq",
    );
    expect(
      await screen.findByText("провайдер ответил 401"),
    ).toBeInTheDocument();
    await userEvent.type(
      screen.getByRole("textbox", { name: "Модель opencode" }),
      "вручную",
    );
    expect(
      screen.getByRole("textbox", { name: "Модель opencode" }),
    ).toHaveValue("вручную");
  });

  it("исполнители: сообщает про restart_required при сохранении дефолтов", async () => {
    const api = fakeApi({
      providers: twoProviders(),
      executorDefaults: vi.fn().mockResolvedValue({
        path: "",
        lines: [],
        changes: 1,
        restart_required: true,
      }),
    });
    render(<SettingsScreen api={api} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Исполнители" }),
    );

    await userEvent.type(
      await screen.findByRole("textbox", { name: "Модель claude-code" }),
      "opus",
    );
    const saves = screen.getAllByRole("button", { name: "Сохранить" });
    await userEvent.click(saves[saves.length - 1]);

    expect(
      await screen.findByText(/вступит в силу.*текущ.*запуск/i),
    ).toBeInTheDocument();
  });
});
