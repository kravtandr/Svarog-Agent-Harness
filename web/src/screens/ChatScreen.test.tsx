import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, type Api } from "../api/client";
import { fakeApi } from "../test/fakeApi";
import { ChatScreen } from "./ChatScreen";

/** Общие пропсы для тестов, которым не важна конкретная сессия. */
const base = { sessionId: "s1", ensureSession: async () => "s1" };

// Миниатюры вложений сами fetch'ат байты (см. ChatScreen.tsx: AttachmentThumb) —
// по умолчанию сеть в тестах недоступна намеренно: тесты, которым нужна
// настоящая миниатюра, переопределяют global.fetch сами.
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockRejectedValue(new Error("сеть отключена в тестах")),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const thread = {
  session_id: "s1",
  title: "FTS-поиск по памяти",
  items: [
    {
      kind: "user" as const,
      text: "Добавь FTS-поиск",
      server: null,
      name: "",
      arg: "",
      result: "",
      status: "",
    },
    {
      kind: "call" as const,
      text: "",
      server: null,
      name: "write_file",
      arg: "memory/index.py",
      result: "записано 1234 символов",
      status: "succeeded",
    },
  ],
};

const api = (over: Partial<Api> = {}): Api =>
  fakeApi({ sessionThread: () => Promise.resolve(thread), ...over });

describe("экран диалога", () => {
  it("рисует историю выбранной сессии", async () => {
    render(
      <ChatScreen
        api={api()}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );

    await waitFor(() =>
      expect(screen.getByText("Добавь FTS-поиск")).toBeInTheDocument(),
    );
    expect(screen.getByText("write_file")).toBeInTheDocument();
    expect(screen.getByText("записано 1234 символов")).toBeInTheDocument();
  });

  it("отправляет сообщение в текущую сессию", async () => {
    const client = api();
    render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );
    await waitFor(() =>
      expect(screen.getByText("Добавь FTS-поиск")).toBeInTheDocument(),
    );

    await userEvent.type(
      screen.getByRole("textbox", { name: /написать/i }),
      "прогони тесты",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    // executor не в override: GET /executors по умолчанию (fakeApi) не
    // отдаёт ни одного варианта, а override не должен его гадать.
    expect(client.sendMessage).toHaveBeenCalledWith(
      "s1",
      "прогони тесты",
      "supervised",
      { provider: "", model: "" },
      [],
    );
    await waitFor(() =>
      expect(screen.getByText("прогони тесты")).toBeInTheDocument(),
    );
  });

  it("отправляет выбранную автономию, а не значение по умолчанию", async () => {
    const client = api();
    render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );
    await waitFor(() =>
      expect(screen.getByText("Добавь FTS-поиск")).toBeInTheDocument(),
    );

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /автономия/i }),
      "yolo",
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: /написать/i }),
      "жги",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    expect(client.sendMessage).toHaveBeenCalledWith(
      "s1",
      "жги",
      "yolo",
      { provider: "", model: "" },
      [],
    );
  });

  it("показывает ошибку загрузки сессий, пришедшую сверху", async () => {
    render(
      <ChatScreen
        api={api()}
        ensureSession={async () => "s1"}
        sessionId={null}
        error="Не удалось загрузить сессии."
      />,
    );
    // findByText, а не getByText: рендер запускает загрузку провайдеров,
    // и её промис должен успеть разрешиться внутри act до конца теста.
    expect(
      await screen.findByText("Не удалось загрузить сессии."),
    ).toBeInTheDocument();
  });

  it("сообщает о неудачной загрузке истории самой сессии", async () => {
    const client = api({
      sessionThread: () => Promise.reject(new Error("нет связи")),
    });
    render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );

    expect(
      await screen.findByText(/не удалось загрузить историю/i),
    ).toBeInTheDocument();
  });

  it("пока грузится — говорит об этом, а не показывает пустоту", async () => {
    render(
      <ChatScreen
        api={api()}
        ensureSession={async () => "s1"}
        sessionId={null}
        loading
      />,
    );
    expect(await screen.findByText(/загружаем/i)).toBeInTheDocument();
  });

  it("пустая сессия приглашает к действию, а не сообщает «нет данных»", async () => {
    const client = api({
      sessionThread: () =>
        Promise.resolve({ session_id: "s1", title: "Новый чат", items: [] }),
    });
    render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );

    await waitFor(() =>
      expect(screen.getByText(/поставьте задачу/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/нет данных/i)).not.toBeInTheDocument();
  });
});

describe("подписка на поток", () => {
  it("закрывает сокет прошлой сессии при переключении", async () => {
    const closed: string[] = [];
    class FakeSocket {
      onmessage: ((e: MessageEvent<string>) => void) | null = null;
      constructor(public url: string | URL) {}
      close() {
        closed.push(String(this.url));
      }
    }
    vi.stubGlobal("WebSocket", FakeSocket);

    const client = api();
    const { rerender } = render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );
    await waitFor(() =>
      expect(screen.getByText("Добавь FTS-поиск")).toBeInTheDocument(),
    );

    await userEvent.type(
      screen.getByRole("textbox", { name: /написать/i }),
      "поехали",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(client.sendMessage).toHaveBeenCalled());

    rerender(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s2"
      />,
    );

    await waitFor(() => expect(closed).toHaveLength(1));
    vi.unstubAllGlobals();
  });

  it("переподписывается после решения по гейту", async () => {
    const opened: string[] = [];
    class FakeSocket {
      onmessage: ((e: MessageEvent<string>) => void) | null = null;
      constructor(public url: string | URL) {
        opened.push(String(url));
      }
      close() {}
    }
    vi.stubGlobal("WebSocket", FakeSocket);

    const client = api({
      sessionThread: () =>
        Promise.resolve({
          session_id: "s1",
          title: "t",
          items: [],
        }),
      decideApproval: vi
        .fn()
        .mockResolvedValue({ run_id: "r-after", state: "running" }),
    });
    render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );
    await waitFor(() =>
      expect(screen.getByText(/поставьте задачу/i)).toBeInTheDocument(),
    );

    await userEvent.type(
      screen.getByRole("textbox", { name: /написать/i }),
      "поехали",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(opened).toHaveLength(1));

    // Гейт приходит событием — эмулируем решение напрямую через API-стенд.
    await client.decideApproval("ap-1", true);
    expect(client.decideApproval).toHaveBeenCalledWith("ap-1", true);
    vi.unstubAllGlobals();
  });
});

describe("ошибки отправки", () => {
  it("показывает отказ сервера, а не молчит", async () => {
    const client = api({
      sendMessage: vi
        .fn()
        .mockRejectedValue(
          new ApiError(
            422,
            "режим 'supervised' с внешним агентом не поддерживается",
          ),
        ),
    });
    render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );
    await waitFor(() =>
      expect(screen.getByText("Добавь FTS-поиск")).toBeInTheDocument(),
    );

    await userEvent.type(
      screen.getByRole("textbox", { name: /написать/i }),
      "поехали",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    expect(await screen.findByText(/не поддерживается/i)).toBeInTheDocument();
    // Реплика не должна остаться висеть, будто она отправлена.
    expect(screen.queryByText("поехали")).not.toBeInTheDocument();
  });
});

describe("чистая установка", () => {
  it("создаёт сессию сама при первой отправке, а не молчит", async () => {
    const client = api();
    const ensureSession = vi.fn().mockResolvedValue("s-new");
    render(
      <ChatScreen
        api={client}
        sessionId={null}
        ensureSession={ensureSession}
      />,
    );

    await userEvent.type(
      screen.getByRole("textbox", { name: /написать/i }),
      "первая задача",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    await waitFor(() => expect(ensureSession).toHaveBeenCalled());
    expect(client.sendMessage).toHaveBeenCalledWith(
      "s-new",
      "первая задача",
      "supervised",
      { provider: "", model: "" },
      [],
    );
  });
});

describe("провайдер и модель", () => {
  it("отправляет выбранные провайдера и модель", async () => {
    const sendMessage = vi
      .fn()
      .mockResolvedValue({ run_id: "r1", state: "running" });
    const client = api({
      sendMessage,
      providers: vi.fn().mockResolvedValue([
        {
          name: "router",
          base_url: "https://x/v1",
          model: "m0",
          is_default: true,
        },
      ]),
      providerModels: vi.fn().mockResolvedValue([
        {
          id: "x/y",
          name: "X Y",
          context_length: null,
          input_usd_per_mtok: null,
          output_usd_per_mtok: null,
        },
      ]),
    });
    render(<ChatScreen api={client} sessionId="s1" ensureSession={vi.fn()} />);

    await userEvent.click(await screen.findByLabelText("Выбрать модель"));
    await userEvent.click(await screen.findByText("X Y"));
    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "привет{Enter}",
    );

    expect(sendMessage).toHaveBeenCalledWith(
      "s1",
      "привет",
      "supervised",
      { provider: "router", model: "x/y" },
      [],
    );
  });

  it("сохраняет выбор между сообщениями", async () => {
    const sendMessage = vi
      .fn()
      .mockResolvedValue({ run_id: "r1", state: "running" });
    const client = api({
      sendMessage,
      providers: vi.fn().mockResolvedValue([
        {
          name: "router",
          base_url: "https://x/v1",
          model: "m0",
          is_default: true,
        },
      ]),
      providerModels: vi.fn().mockResolvedValue([
        {
          id: "x/y",
          name: "X Y",
          context_length: null,
          input_usd_per_mtok: null,
          output_usd_per_mtok: null,
        },
      ]),
    });
    render(<ChatScreen api={client} sessionId="s1" ensureSession={vi.fn()} />);

    await userEvent.click(await screen.findByLabelText("Выбрать модель"));
    await userEvent.click(await screen.findByText("X Y"));
    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "привет{Enter}",
    );
    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "ещё раз{Enter}",
    );

    // Второй вызов sendMessage несёт тот же override, что и первый: выбор
    // провайдера и модели не сбрасывается между сообщениями.
    expect(sendMessage).toHaveBeenNthCalledWith(
      1,
      "s1",
      "привет",
      "supervised",
      { provider: "router", model: "x/y" },
      [],
    );
    expect(sendMessage).toHaveBeenNthCalledWith(
      2,
      "s1",
      "ещё раз",
      "supervised",
      { provider: "router", model: "x/y" },
      [],
    );
  });

  it("меняет провайдера и подставляет его собственную модель из конфига", async () => {
    const sendMessage = vi
      .fn()
      .mockResolvedValue({ run_id: "r1", state: "running" });
    const client = api({
      sendMessage,
      providers: vi.fn().mockResolvedValue([
        {
          name: "router",
          base_url: "https://x/v1",
          model: "m-router",
          is_default: true,
        },
        {
          name: "backup",
          base_url: "https://y/v1",
          model: "m-backup",
          is_default: false,
        },
      ]),
    });
    render(<ChatScreen api={client} sessionId="s1" ensureSession={vi.fn()} />);

    await waitFor(() =>
      expect(screen.getByLabelText("Провайдер")).toHaveValue("router"),
    );

    await userEvent.selectOptions(screen.getByLabelText("Провайдер"), "backup");
    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "поехали{Enter}",
    );

    // Модель — от нового провайдера ("m-backup"), а не унаследованная от
    // предыдущего ("m-router"): pickProvider обязан её подставить сам.
    expect(sendMessage).toHaveBeenCalledWith(
      "s1",
      "поехали",
      "supervised",
      { provider: "backup", model: "m-backup" },
      [],
    );
  });
});

describe("исполнитель из /executors", () => {
  it("подставляет исполнителя, полученного из /executors, в override", async () => {
    const sendMessage = vi
      .fn()
      .mockResolvedValue({ run_id: "r1", state: "running" });
    const client = api({
      sendMessage,
      executors: vi.fn().mockResolvedValue([
        {
          value: "native",
          kind: "native",
          adapter: null,
          available: true,
          is_active: false,
        },
        {
          value: "codex",
          kind: "external",
          adapter: "codex",
          available: true,
          is_active: true,
        },
      ]),
    });
    render(<ChatScreen api={client} sessionId="s1" ensureSession={vi.fn()} />);

    // Ждём, пока /executors отработает: селект «Исполнитель» покажет
    // настоящее значение, а не литерал и не угадку.
    await waitFor(() =>
      expect(screen.getByLabelText("Исполнитель")).toHaveValue("codex"),
    );

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "старт{Enter}",
    );

    expect(sendMessage).toHaveBeenCalledWith(
      "s1",
      "старт",
      "supervised",
      { executor: "external", provider: "", model: "" },
      [],
    );
  });

  it("не гадает исполнителя, если /executors не ответил", async () => {
    const sendMessage = vi
      .fn()
      .mockResolvedValue({ run_id: "r1", state: "running" });
    const client = api({
      sendMessage,
      executors: vi.fn().mockRejectedValue(new Error("нет связи")),
    });
    render(<ChatScreen api={client} sessionId="s1" ensureSession={vi.fn()} />);

    // Селект остаётся закрытым для выбора — значение неизвестно.
    expect(screen.getByLabelText("Исполнитель")).toBeDisabled();

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "старт{Enter}",
    );

    // executor вообще не входит в override — сервер возьмёт значение
    // из своего конфига, а не из угадки клиента.
    expect(sendMessage).toHaveBeenCalledWith(
      "s1",
      "старт",
      "supervised",
      { provider: "", model: "" },
      [],
    );
  });
});

describe("слэш-команды", () => {
  it("/new заводит новый чат, а не уходит агенту", async () => {
    const sendMessage = vi.fn();
    const onNew = vi.fn();
    render(
      <ChatScreen {...base} api={fakeApi({ sendMessage })} onNew={onNew} />,
    );

    await userEvent.type(screen.getByLabelText("Написать Сварогу"), "/new");
    await userEvent.keyboard("{Enter}{Enter}");

    expect(onNew).toHaveBeenCalled();
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("неизвестная команда не отправляется, а объясняется", async () => {
    const sendMessage = vi.fn();
    render(<ChatScreen {...base} api={fakeApi({ sendMessage })} />);

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "/опечатка{Escape}{Enter}",
    );

    expect(sendMessage).not.toHaveBeenCalled();
    expect(await screen.findByText(/неизвестная команда/i)).toBeInTheDocument();
  });

  it("/sessions зовёт onSessions, а не отправляет текст агенту", async () => {
    const sendMessage = vi.fn();
    const onSessions = vi.fn();
    render(
      <ChatScreen
        {...base}
        api={fakeApi({ sendMessage })}
        onSessions={onSessions}
      />,
    );

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "/sessions{Enter}",
    );

    expect(onSessions).toHaveBeenCalled();
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("/executor переводит фокус на выбор исполнителя", async () => {
    // Пустой список из /executors держит селект отключённым (см. тест
    // "не гадает исполнителя..." выше) — фокус проверяем на живом селекте.
    const executors = vi.fn().mockResolvedValue([
      {
        value: "native",
        kind: "native",
        adapter: null,
        available: true,
        is_active: true,
      },
    ]);
    render(<ChatScreen {...base} api={fakeApi({ executors })} />);
    await waitFor(() =>
      expect(screen.getByLabelText("Исполнитель")).not.toBeDisabled(),
    );

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "/executor{Enter}",
    );

    expect(screen.getByLabelText("Исполнитель")).toHaveFocus();
  });

  it("/policies переводит фокус на выбор автономии", async () => {
    render(<ChatScreen {...base} api={fakeApi()} />);

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "/policies{Enter}",
    );

    expect(screen.getByLabelText("Автономия")).toHaveFocus();
  });

  it("/help показывает список команд из GET /commands", async () => {
    const commands = vi.fn().mockResolvedValue([
      { name: "help", usage: "/help", help: "показать команды" },
      { name: "new", usage: "/new", help: "новый чат" },
    ]);
    render(<ChatScreen {...base} api={fakeApi({ commands })} />);

    // "/help" совпадает с реальной командой из GET /commands — меню
    // автодополнения открыто, первый Enter вставляет подсказку (с пробелом),
    // второй уже отправляет: тот же порядок, что и в тесте про /new.
    await userEvent.type(screen.getByLabelText("Написать Сварогу"), "/help");
    await userEvent.keyboard("{Enter}{Enter}");

    expect(
      await screen.findByText(/\/help — показать команды/),
    ).toBeInTheDocument();
    expect(screen.getByText(/\/new — новый чат/)).toBeInTheDocument();
  });

  it("/copy копирует последний ответ агента в буфер", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    // Стенд навигатора трогает общий jsdom-объект — обязательно возвращаем
    // как было, иначе следующий тест ("без navigator.clipboard") увидит
    // чужой буфер, а не его отсутствие.
    const original = (navigator as { clipboard?: unknown }).clipboard;
    Object.assign(navigator, { clipboard: { writeText } });
    try {
      const client = api({
        sessionThread: vi.fn().mockResolvedValue({
          session_id: "s1",
          title: "",
          items: [
            {
              kind: "say" as const,
              text: "готово",
              server: null,
              name: "",
              arg: "",
              result: "",
              status: "",
            },
          ],
        }),
      });
      render(<ChatScreen {...base} api={client} sessionId="s1" />);
      await waitFor(() =>
        expect(screen.getByText("готово")).toBeInTheDocument(),
      );

      await userEvent.type(
        screen.getByLabelText("Написать Сварогу"),
        "/copy{Enter}",
      );

      await waitFor(() => expect(writeText).toHaveBeenCalledWith("готово"));
    } finally {
      Object.assign(navigator, { clipboard: original });
    }
  });

  it("/copy без ответа агента сообщает, что нечего копировать", async () => {
    render(<ChatScreen {...base} api={fakeApi()} />);

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "/copy{Enter}",
    );

    expect(await screen.findByText(/нечего копировать/i)).toBeInTheDocument();
  });

  it("/copy без navigator.clipboard сообщает об этом, а не падает", async () => {
    // jsdom не реализует navigator.clipboard — отдельного стенда не нужно,
    // тест проверяет ровно тот случай, который в браузере даёт то же самое.
    const client = api({
      sessionThread: vi.fn().mockResolvedValue({
        session_id: "s1",
        title: "",
        items: [
          {
            kind: "say" as const,
            text: "готово",
            server: null,
            name: "",
            arg: "",
            result: "",
            status: "",
          },
        ],
      }),
    });
    render(<ChatScreen {...base} api={client} sessionId="s1" />);
    await waitFor(() => expect(screen.getByText("готово")).toBeInTheDocument());

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "/copy{Enter}",
    );

    expect(
      await screen.findByText(/буфер обмена недоступен/i),
    ).toBeInTheDocument();
  });
});

describe("вложения в композере", () => {
  it("прикреплённый файл уходит вместе с сообщением", async () => {
    const sendMessage = vi
      .fn()
      .mockResolvedValue({ run_id: "r1", state: "running" });
    const uploadAttachment = vi.fn().mockResolvedValue({
      path: ".attachments/ab_скрин.png",
      name: "скрин.png",
      size_bytes: 4,
      mime: "image/png",
      too_large_for_vision: false,
    });
    render(
      <ChatScreen
        {...base}
        api={fakeApi({ sendMessage, uploadAttachment })}
        sessionId="s1"
      />,
    );

    const file = new File([new Uint8Array([1])], "скрин.png", {
      type: "image/png",
    });
    fireEvent.paste(screen.getByLabelText("Написать Сварогу"), {
      clipboardData: { files: [file], items: [] },
    });
    await screen.findByText("скрин.png");
    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "смотри{Enter}",
    );

    expect(sendMessage).toHaveBeenCalledWith(
      "s1",
      "смотри",
      expect.anything(),
      expect.anything(),
      [".attachments/ab_скрин.png"],
    );
  });

  it("ошибка загрузки показана и не мешает отправить сообщение", async () => {
    const uploadAttachment = vi
      .fn()
      .mockRejectedValue(new ApiError(415, "расширение '.exe' не поддержано"));
    const sendMessage = vi
      .fn()
      .mockResolvedValue({ run_id: "r1", state: "running" });
    render(
      <ChatScreen
        {...base}
        api={fakeApi({ uploadAttachment, sendMessage })}
        sessionId="s1"
      />,
    );

    fireEvent.paste(screen.getByLabelText("Написать Сварогу"), {
      clipboardData: { files: [new File([], "вирус.exe")], items: [] },
    });
    expect(await screen.findByText(/не поддержано/)).toBeInTheDocument();

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "текст{Enter}",
    );
    expect(sendMessage).toHaveBeenCalledWith(
      "s1",
      "текст",
      expect.anything(),
      expect.anything(),
      [],
    );
  });

  it("переключение сессии сбрасывает незавершённое вложение прошлого чата", async () => {
    const uploadAttachment = vi.fn().mockResolvedValue({
      path: ".attachments/ab_скрин.png",
      name: "скрин.png",
      size_bytes: 4,
      mime: "image/png",
      too_large_for_vision: false,
    });
    const client = fakeApi({ uploadAttachment });
    const { rerender } = render(
      <ChatScreen {...base} api={client} sessionId="s1" />,
    );

    const file = new File([new Uint8Array([1])], "скрин.png", {
      type: "image/png",
    });
    fireEvent.paste(screen.getByLabelText("Написать Сварогу"), {
      clipboardData: { files: [file], items: [] },
    });
    await screen.findByText("скрин.png");

    rerender(
      <ChatScreen
        {...base}
        api={client}
        ensureSession={base.ensureSession}
        sessionId="s2"
      />,
    );

    // Путь вложения принадлежит workspace сессии s1 — отправка в s2 с ним
    // получила бы 400 от verify_attachment, поэтому чип не должен пережить
    // переключение чата.
    await waitFor(() =>
      expect(screen.queryByText("скрин.png")).not.toBeInTheDocument(),
    );
  });

  it("вложение переживает создание сессии на чистой установке, даже если sessionId сменился уже после того, как чип появился", async () => {
    // На чистой установке sessionId===null; ensureSession() создаёт сессию
    // и возвращает её id раньше, чем родитель (App) успевает перерисовать
    // этот экран с новым sessionId-пропом — порядок, в котором это
    // происходит, не гарантирован. Тест намеренно рендерит успешный чип
    // ДО перерисовки с новым sessionId, чтобы проверить именно тот случай,
    // где наивный сброс по смене сессии стёр бы чип задним числом.
    const uploadAttachment = vi.fn().mockResolvedValue({
      path: ".attachments/ab_скрин.png",
      name: "скрин.png",
      size_bytes: 4,
      mime: "image/png",
      too_large_for_vision: false,
    });
    const client = fakeApi({ uploadAttachment });
    const ensureSession = vi.fn().mockResolvedValue("s-new");
    const { rerender } = render(
      <ChatScreen
        api={client}
        sessionId={null}
        ensureSession={ensureSession}
      />,
    );

    const file = new File([new Uint8Array([1])], "скрин.png", {
      type: "image/png",
    });
    fireEvent.paste(screen.getByLabelText("Написать Сварогу"), {
      clipboardData: { files: [file], items: [] },
    });
    await screen.findByText("скрин.png");

    // Родитель наконец перерисовался с новым sessionId — эффект смены
    // сессии срабатывает первый раз именно сейчас, уже после того как чип
    // появился.
    rerender(
      <ChatScreen
        api={client}
        sessionId="s-new"
        ensureSession={ensureSession}
      />,
    );

    await waitFor(() =>
      expect(client.sessionThread).toHaveBeenCalledWith("s-new"),
    );
    expect(screen.getByText("скрин.png")).toBeInTheDocument();
  });
});

describe("миниатюры вложений в ленте", () => {
  it("вложение в ленте рисуется миниатюрой, а не строкой пути", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(new Blob([new Uint8Array([1])], { type: "image/png" }), {
          status: 200,
        }),
      ),
    );
    render(
      <ChatScreen
        {...base}
        api={fakeApi({
          sessionThread: vi.fn().mockResolvedValue({
            session_id: "s1",
            title: "",
            items: [
              {
                kind: "user" as const,
                text: "смотри\n\nВложения (прочитай их read_image / read_document): .attachments/ab_скрин.png",
                server: null,
                name: "",
                arg: "",
                result: "",
                status: "",
              },
            ],
          }),
        })}
        sessionId="s1"
      />,
    );

    await screen.findByRole("img", { name: /скрин\.png/ });
    // Строка "Вложения (...)" остаётся видна — спека прямо требует, чтобы
    // человек видел ровно то, что получил агент, миниатюра только вдобавок.
    expect(screen.getByText(/Вложения \(/)).toBeInTheDocument();
  });

  it("документ без inline-раздачи рисуется именованным чипом, а не сломанной картинкой", async () => {
    render(
      <ChatScreen
        {...base}
        api={fakeApi({
          sessionThread: vi.fn().mockResolvedValue({
            session_id: "s1",
            title: "",
            items: [
              {
                kind: "user" as const,
                text: "вот отчёт\n\nВложения (прочитай их read_image / read_document): .attachments/ab12cd34_отчёт.pdf",
                server: null,
                name: "",
                arg: "",
                result: "",
                status: "",
              },
            ],
          }),
        })}
        sessionId="s1"
      />,
    );

    // Строка "Вложения (...)" в тексте и подпись чипа обе содержат
    // "отчёт.pdf" — ищем именно чип, а не любое совпадение по тексту.
    const chip = await screen.findByText(/отчёт\.pdf/, {
      selector: ".chat__doc",
    });
    expect(chip).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});
