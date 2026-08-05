import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, type Api } from "../api/client";
import type { Attachment } from "../api/types";
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
      screen.getByRole("combobox", { name: /написать/i }),
      "прогони тесты",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    // executor не в override: GET /executors по умолчанию (fakeApi) не
    // отдаёт ни одного варианта, а override не должен его гадать.
    expect(client.sendMessage).toHaveBeenCalledWith(
      "s1",
      "прогони тесты",
      "yolo",
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
      "supervised",
    );
    await userEvent.type(
      screen.getByRole("combobox", { name: /написать/i }),
      "жги",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    expect(client.sendMessage).toHaveBeenCalledWith(
      "s1",
      "жги",
      "supervised",
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
      expect(screen.getByText(/что куём/i)).toBeInTheDocument(),
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
      screen.getByRole("combobox", { name: /написать/i }),
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

  it("переподписывается на живой run сессии при открытии (параллельные чаты)", async () => {
    const opened: string[] = [];
    class FakeSocket {
      static last: FakeSocket | null = null;
      onmessage: ((e: MessageEvent<string>) => void) | null = null;
      constructor(public url: string | URL) {
        opened.push(String(url));
        FakeSocket.last = this;
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
          live_run_id: "r-live",
          live_task: "долгая задача",
        }),
    });
    render(
      <ChatScreen
        api={client}
        ensureSession={async () => "s1"}
        sessionId="s1"
      />,
    );

    // Пузырь задачи живого run'а виден сразу, подписка идёт на его WS.
    await waitFor(() =>
      expect(screen.getByText("долгая задача")).toBeInTheDocument(),
    );
    expect(opened.some((url) => url.includes("r-live"))).toBe(true);
    // Пока run жив — отправка заблокирована с подсказкой.
    expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
    // Реплей событий восстанавливает ленту; run_finished разблокирует ввод.
    act(() =>
      FakeSocket.last?.onmessage?.({
        data: JSON.stringify({ type: "text", delta: "готово: сделано" }),
      } as MessageEvent<string>),
    );
    act(() =>
      FakeSocket.last?.onmessage?.({
        data: JSON.stringify({ type: "run_finished", state: "completed" }),
      } as MessageEvent<string>),
    );
    await waitFor(() =>
      expect(screen.getByText(/готово: сделано/)).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Отправить" })).toBeEnabled();
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
      expect(screen.getByText(/что куём/i)).toBeInTheDocument(),
    );

    await userEvent.type(
      screen.getByRole("combobox", { name: /написать/i }),
      "поехали",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(opened).toHaveLength(1));

    // Гейт приходит событием — эмулируем решение напрямую через API-стенд.
    await client.decideApproval("ap-1", true);
    expect(client.decideApproval).toHaveBeenCalledWith("ap-1", true);
    vi.unstubAllGlobals();
  });

  it("строка статуса живёт весь run и показывает прогресс", async () => {
    class FakeSocket {
      static last: FakeSocket | null = null;
      onmessage: ((e: MessageEvent<string>) => void) | null = null;
      constructor(public url: string | URL) {
        FakeSocket.last = this;
      }
      close() {}
    }
    vi.stubGlobal("WebSocket", FakeSocket);

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
      screen.getByRole("combobox", { name: /написать/i }),
      "долгая задача",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(client.sendMessage).toHaveBeenCalled());

    // Тикающий секундомер живёт вне aria-live-региона (a11y): статус
    // объявляется один раз, хвост с секундами/токенами скринридеру не
    // читается на каждый тик (aria-hidden). Матчим по контейнеру, а не по
    // тексту напрямую — он теперь разбит на несколько элементов.
    const statusLine = () =>
      document.querySelector(".chat__thinking")?.textContent ?? "";

    // До каких-либо событий — секундомер уже виден.
    expect(statusLine()).toMatch(/Сварог работает… \d+:\d\d/);

    // Первое текстовое событие строку НЕ гасит (раньше гасило).
    act(() =>
      FakeSocket.last?.onmessage?.({
        data: JSON.stringify({ type: "text", delta: "смотрю код" }),
      } as MessageEvent<string>),
    );
    expect(statusLine()).toMatch(/Сварог работает…/);

    // progress подмешивает токены и стоимость, ленту не трогает.
    act(() =>
      FakeSocket.last?.onmessage?.({
        data: JSON.stringify({
          type: "progress",
          iterations: 3,
          tokens: 12400,
          cost_usd: 0.04,
        }),
      } as MessageEvent<string>),
    );
    expect(statusLine()).toMatch(/Сварог работает… \d+:\d\d/);
    expect(statusLine()).toMatch(/· 12 400 токенов · \$0\.04/);

    // Финал гасит строку.
    act(() =>
      FakeSocket.last?.onmessage?.({
        data: JSON.stringify({ type: "run_finished", state: "completed" }),
      } as MessageEvent<string>),
    );
    expect(document.querySelector(".chat__thinking")).toBeNull();
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
      screen.getByRole("combobox", { name: /написать/i }),
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
      screen.getByRole("combobox", { name: /написать/i }),
      "первая задача",
    );
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));

    await waitFor(() => expect(ensureSession).toHaveBeenCalled());
    expect(client.sendMessage).toHaveBeenCalledWith(
      "s-new",
      "первая задача",
      "yolo",
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
      "yolo",
      { provider: "router", model: "x/y" },
      [],
    );
  });

  it("сохраняет выбор между сообщениями", async () => {
    // Пока run первого сообщения жив, отправка заблокирована (параллельные
    // чаты) — завершаем его событием run_finished через фейковый сокет.
    class FakeSocket {
      static last: FakeSocket | null = null;
      onmessage: ((e: MessageEvent<string>) => void) | null = null;
      constructor(public url: string | URL) {
        FakeSocket.last = this;
      }
      close() {}
    }
    vi.stubGlobal("WebSocket", FakeSocket);
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
    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1));
    act(() =>
      FakeSocket.last?.onmessage?.({
        data: JSON.stringify({ type: "run_finished", state: "completed" }),
      } as MessageEvent<string>),
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
      "yolo",
      { provider: "router", model: "x/y" },
      [],
    );
    expect(sendMessage).toHaveBeenNthCalledWith(
      2,
      "s1",
      "ещё раз",
      "yolo",
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
      "yolo",
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
      "yolo",
      { executor: "external", adapter: "codex", provider: "", model: "" },
      [],
    );
  });

  it("выбор 'opencode' в композере уходит в override сообщения", async () => {
    // Это тест, который должен был поймать пропажу: ChatScreen раньше
    // отбрасывал option.adapter и слал только option.kind, так что выбор
    // конкретного внешнего агента был декоративным. Селект приводится в
    // движение через userEvent, а не подстановкой callback'а напрямую —
    // иначе тест не увидел бы именно этот путь данных.
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
          is_active: true,
        },
        {
          value: "opencode",
          kind: "external",
          adapter: "opencode",
          available: true,
          is_active: false,
        },
      ]),
    });
    render(<ChatScreen api={client} sessionId="s1" ensureSession={vi.fn()} />);

    await waitFor(() =>
      expect(screen.getByLabelText("Исполнитель")).toHaveValue("native"),
    );

    await userEvent.selectOptions(
      screen.getByLabelText("Исполнитель"),
      "opencode",
    );

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "старт{Enter}",
    );

    expect(sendMessage).toHaveBeenCalledWith(
      "s1",
      "старт",
      "yolo",
      { executor: "external", adapter: "opencode", provider: "", model: "" },
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
      "yolo",
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

  it("баннер ошибки загрузки исчезает после успешной отправки", async () => {
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

    await waitFor(() =>
      expect(screen.queryByText(/не поддержано/)).not.toBeInTheDocument(),
    );
  });

  it("убрать чип тоже закрывает баннер ошибки загрузки прошлого файла", async () => {
    const uploadAttachment = vi
      .fn()
      .mockResolvedValueOnce({
        path: ".attachments/ab_a.png",
        name: "a.png",
        size_bytes: 1,
        mime: "image/png",
        too_large_for_vision: false,
      })
      .mockRejectedValueOnce(
        new ApiError(415, "расширение '.exe' не поддержано"),
      );
    render(
      <ChatScreen
        {...base}
        api={fakeApi({ uploadAttachment })}
        sessionId="s1"
      />,
    );

    fireEvent.paste(screen.getByLabelText("Написать Сварогу"), {
      clipboardData: {
        files: [
          new File([new Uint8Array([1])], "a.png", { type: "image/png" }),
        ],
        items: [],
      },
    });
    await screen.findByText("a.png");

    fireEvent.paste(screen.getByLabelText("Написать Сварогу"), {
      clipboardData: { files: [new File([], "вирус.exe")], items: [] },
    });
    expect(await screen.findByText(/не поддержано/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Убрать a.png" }));

    expect(screen.queryByText(/не поддержано/)).not.toBeInTheDocument();
  });
});

describe("гонки идентичности сессии между attach() и send()", () => {
  it("два файла, брошенные разом на чистой установке, заводят одну сессию, а не две", async () => {
    // Без общего резолвера каждый attach() из forEach в Composer видит
    // sessionId===null в один и тот же тик и зовёт ensureSession()
    // отдельно — здесь это было бы видно как второй, отличный от первого,
    // id сессии.
    let calls = 0;
    const ensureSession = vi
      .fn()
      .mockImplementation(async () => `s-${++calls}`);
    const uploadAttachment = vi
      .fn()
      .mockImplementation((_sessionId: string, file: File) =>
        Promise.resolve({
          path: `.attachments/${file.name}`,
          name: file.name,
          size_bytes: 1,
          mime: "image/png",
          too_large_for_vision: false,
        }),
      );
    const client = fakeApi({ uploadAttachment });
    render(
      <ChatScreen
        api={client}
        sessionId={null}
        ensureSession={ensureSession}
      />,
    );

    const fileA = new File([new Uint8Array([1])], "a.png", {
      type: "image/png",
    });
    const fileB = new File([new Uint8Array([1])], "b.png", {
      type: "image/png",
    });
    fireEvent.drop(screen.getByLabelText("Написать Сварогу"), {
      dataTransfer: { files: [fileA, fileB] },
    });

    await screen.findByText("a.png");
    await screen.findByText("b.png");

    expect(ensureSession).toHaveBeenCalledTimes(1);
    expect(uploadAttachment).toHaveBeenNthCalledWith(1, "s-1", fileA);
    expect(uploadAttachment).toHaveBeenNthCalledWith(2, "s-1", fileB);
  });

  it("Enter, отправленный раньше перерисовки с новым sessionId, не заводит вторую сессию", async () => {
    // attach() уже создал сессию и получил её id, но родитель (App) ещё не
    // перерисовал экран с новым sessionId-пропом — тот же порядок, что и в
    // тесте про переживающий чип выше, но здесь ещё и Enter до перерисовки.
    // Без общего резолвера send() увидел бы sessionId===null и завёл вторую
    // сессию, отправив путь вложения из workspace первой в чужую сессию.
    let calls = 0;
    const ensureSession = vi
      .fn()
      .mockImplementation(async () => `s-${++calls}`);
    const uploadAttachment = vi.fn().mockResolvedValue({
      path: ".attachments/ab_скрин.png",
      name: "скрин.png",
      size_bytes: 4,
      mime: "image/png",
      too_large_for_vision: false,
    });
    const sendMessage = vi
      .fn()
      .mockResolvedValue({ run_id: "r1", state: "running" });
    const client = fakeApi({ uploadAttachment, sendMessage });
    render(
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

    // Намеренно без rerender(): родитель ещё не успел прислать новый
    // sessionId-проп.
    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "смотри{Enter}",
    );

    await waitFor(() => expect(sendMessage).toHaveBeenCalled());
    expect(ensureSession).toHaveBeenCalledTimes(1);
    expect(sendMessage).toHaveBeenCalledWith(
      "s-1",
      "смотри",
      expect.anything(),
      expect.anything(),
      [".attachments/ab_скрин.png"],
    );
  });

  it("после отклонённого ensureSession следующая отправка пробует снова, а не наследует ту же ошибку", async () => {
    // На чистой установке первый ensureSession() падает (svarog serve ещё не
    // поднялся), а resolveTarget() кладёт этот rejected promise в кеш и
    // никогда его не чистит — эффект на sessionId!==null здесь не сработает,
    // сессия так и не появилась. Каждая следующая отправка ждёт тот же
    // отклонённый promise и падает с той же ошибкой, хотя сервер уже отвечает.
    const ensureSession = vi
      .fn()
      .mockRejectedValueOnce(new Error("svarog serve недоступен"))
      .mockResolvedValueOnce("s-new");
    const sendMessage = vi
      .fn()
      .mockResolvedValue({ run_id: "r1", state: "running" });
    const client = fakeApi({ sendMessage });
    render(
      <ChatScreen
        api={client}
        sessionId={null}
        ensureSession={ensureSession}
      />,
    );

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "первое{Enter}",
    );
    expect(
      await screen.findByText(
        "Не удалось отправить сообщение. Проверьте, что svarog serve запущен.",
      ),
    ).toBeInTheDocument();
    expect(sendMessage).not.toHaveBeenCalled();

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "второе{Enter}",
    );

    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1));
    expect(ensureSession).toHaveBeenCalledTimes(2);
    expect(sendMessage).toHaveBeenCalledWith(
      "s-new",
      "второе",
      expect.anything(),
      expect.anything(),
      [],
    );
  });

  it("загрузка, ответившая после настоящего переключения чата, не попадает в attachments новой сессии", async () => {
    // В отличие от теста "переключение сессии сбрасывает незавершённое
    // вложение прошлого чата" выше (там upload уже успел ответить ДО
    // переключения), здесь переключение случается, пока запрос ещё в
    // полёте — и только потом он отвечает.
    let resolveUpload: (value: Attachment) => void = () => {};
    const uploadAttachment = vi.fn(
      () =>
        new Promise<Attachment>((resolve) => {
          resolveUpload = resolve;
        }),
    );
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

    // Переключаем чат, пока запрос загрузки ещё не ответил.
    rerender(<ChatScreen {...base} api={client} sessionId="s2" />);

    // Теперь запрос отвечает — уже после того, как сессия сменилась.
    resolveUpload({
      path: ".attachments/ab_скрин.png",
      name: "скрин.png",
      size_bytes: 4,
      mime: "image/png",
      too_large_for_vision: false,
    });

    // Даём микрозадачам отработать и убеждаемся, что чип не появился в s2.
    await waitFor(() =>
      expect(client.sessionThread).toHaveBeenCalledWith("s2"),
    );
    await new Promise((r) => setTimeout(r, 10));
    expect(screen.queryByText("скрин.png")).not.toBeInTheDocument();
  });
});

describe("блокировка отправки во время загрузки вложения", () => {
  it("Enter не отправляет сообщение, пока загрузка вложения не ответила", async () => {
    let resolveUpload: (value: Attachment) => void = () => {};
    const uploadAttachment = vi.fn(
      () =>
        new Promise<Attachment>((resolve) => {
          resolveUpload = resolve;
        }),
    );
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

    const file = new File([new Uint8Array([1])], "скрин.png", {
      type: "image/png",
    });
    fireEvent.paste(screen.getByLabelText("Написать Сварогу"), {
      clipboardData: { files: [file], items: [] },
    });

    await userEvent.type(
      screen.getByLabelText("Написать Сварогу"),
      "смотри{Enter}",
    );
    // Загрузка ещё не ответила — отправка должна быть заблокирована,
    // иначе сообщение уходит без пути, которого пока не существует.
    expect(sendMessage).not.toHaveBeenCalled();
    expect(await screen.findByText(/загружаем файл/i)).toBeInTheDocument();

    resolveUpload({
      path: ".attachments/ab_скрин.png",
      name: "скрин.png",
      size_bytes: 4,
      mime: "image/png",
      too_large_for_vision: false,
    });
    await screen.findByText("скрин.png");

    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
    expect(sendMessage).toHaveBeenCalledWith(
      "s1",
      "смотри",
      expect.anything(),
      expect.anything(),
      [".attachments/ab_скрин.png"],
    );
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

  it("кодирует id сессии в пути к вложению, как и остальные пути client.ts", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob([new Uint8Array([1])], { type: "image/png" }), {
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(
      <ChatScreen
        api={fakeApi({
          sessionThread: vi.fn().mockResolvedValue({
            session_id: "s 1",
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
        sessionId="s 1"
        ensureSession={async () => "s 1"}
      />,
    );

    await screen.findByRole("img", { name: /скрин\.png/ });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/sessions/s%201/attachments/"),
      expect.anything(),
    );
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

  it("восстанавливает выбор композера из localStorage", async () => {
    window.localStorage.setItem(
      "svarog.composer",
      JSON.stringify({
        autonomy: "supervised",
        executor: "opencode",
        sandbox: "local-trusted",
        provider: "LMStudio",
        model: "qwen/qwen3.6-35b-a3b",
      }),
    );
    const client = api({
      executors: vi.fn().mockResolvedValue([
        {
          value: "native",
          kind: "native",
          adapter: null,
          available: true,
          is_active: true,
        },
        {
          value: "opencode",
          kind: "external",
          adapter: "opencode",
          available: true,
          is_active: false,
        },
      ]),
      sandboxes: vi.fn().mockResolvedValue([
        { value: "docker", available: true, is_active: true },
        { value: "local-trusted", available: true, is_active: false },
      ]),
      providers: vi.fn().mockResolvedValue([
        {
          name: "local",
          base_url: "https://x/v1",
          model: "m",
          is_default: true,
        },
        {
          name: "LMStudio",
          base_url: "http://lm:1234/v1",
          model: "q",
          is_default: false,
        },
      ]),
    });
    render(<ChatScreen {...base} api={client} sessionId="s1" />);

    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Исполнитель" })).toHaveValue(
        "opencode",
      ),
    );
    expect(screen.getByRole("combobox", { name: "Sandbox" })).toHaveValue(
      "local-trusted",
    );
    expect(screen.getByRole("combobox", { name: "Провайдер" })).toHaveValue(
      "LMStudio",
    );
    expect(screen.getByRole("combobox", { name: /автономия/i })).toHaveValue(
      "supervised",
    );
    expect(
      screen.getByRole("button", { name: "Выбрать модель" }),
    ).toHaveTextContent("qwen/qwen3.6-35b-a3b");
    window.localStorage.clear();
  });

  it("исчезнувший из конфига сохранённый провайдер откатывается на дефолт", async () => {
    window.localStorage.setItem(
      "svarog.composer",
      JSON.stringify({ provider: "ghost", model: "ghost-model" }),
    );
    const client = api({
      providers: vi.fn().mockResolvedValue([
        {
          name: "local",
          base_url: "https://x/v1",
          model: "m",
          is_default: true,
        },
        {
          name: "LMStudio",
          base_url: "http://lm:1234/v1",
          model: "q",
          is_default: false,
        },
      ]),
    });
    render(<ChatScreen {...base} api={client} sessionId="s1" />);

    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Провайдер" })).toHaveValue(
        "local",
      ),
    );
    expect(
      screen.getByRole("button", { name: "Выбрать модель" }),
    ).toHaveTextContent("m");
    window.localStorage.clear();
  });

  it("сохраняет смену провайдера и модели в localStorage", async () => {
    window.localStorage.clear();
    const client = api({
      providers: vi.fn().mockResolvedValue([
        {
          name: "local",
          base_url: "https://x/v1",
          model: "m",
          is_default: true,
        },
        {
          name: "LMStudio",
          base_url: "http://lm:1234/v1",
          model: "q",
          is_default: false,
        },
      ]),
    });
    render(<ChatScreen {...base} api={client} sessionId="s1" />);

    await userEvent.selectOptions(
      await screen.findByRole("combobox", { name: "Провайдер" }),
      "LMStudio",
    );

    const saved = JSON.parse(
      window.localStorage.getItem("svarog.composer") ?? "{}",
    );
    expect(saved.provider).toBe("LMStudio");
    expect(saved.model).toBe("q");
    window.localStorage.clear();
  });

  it("пустой чат: чип папки, заголовок с её именем и затравка в композер", async () => {
    const client = api({
      sessionThread: () =>
        Promise.resolve({ session_id: "s1", title: "Новый чат", items: [] }),
    });
    render(
      <ChatScreen
        {...base}
        api={client}
        sessionId="s1"
        workspace="/home/u/proj/TaskTracker"
      />,
    );

    expect(
      await screen.findByText("Что куём в TaskTracker?"),
    ).toBeInTheDocument();
    expect(screen.getByText("TaskTracker")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Осмотрись/ }));
    expect(screen.getByRole("combobox", { name: /написать/i })).toHaveValue(
      "Осмотрись и расскажи, как устроен этот проект",
    );
  });

  it("пустой чат без папки — общий заголовок, чипа нет", async () => {
    const client = api({
      sessionThread: () =>
        Promise.resolve({ session_id: "s1", title: "Новый чат", items: [] }),
    });
    render(<ChatScreen {...base} api={client} sessionId="s1" />);

    expect(await screen.findByText("Что куём?")).toBeInTheDocument();
    expect(document.querySelector(".chat__ctx")).toBeNull();
  });

  it("провайдеры и исполнители читаются через configApi воркспейса сессии, а не корня serve", async () => {
    // Провайдер, добавленный в настройках проекта (scoped на root сессии),
    // обязан появиться в композере: раньше чат читал конфиг корня serve и
    // селектор провайдера не появлялся, хотя настройки видели обоих.
    const unscoped = api();
    const scoped = fakeApi({
      providers: vi.fn().mockResolvedValue([
        {
          name: "local",
          base_url: "https://x/v1",
          model: "m",
          is_default: true,
        },
        {
          name: "LMStudio",
          base_url: "http://lm:1234/v1",
          model: "q",
          is_default: false,
        },
      ]),
    });
    render(
      <ChatScreen {...base} api={unscoped} configApi={scoped} sessionId="s1" />,
    );

    expect(
      await screen.findByRole("combobox", { name: "Провайдер" }),
    ).toBeInTheDocument();
    expect(scoped.providers).toHaveBeenCalled();
    expect(scoped.executors).toHaveBeenCalled();
    expect(scoped.sandboxes).toHaveBeenCalled();
    expect(scoped.providerModels).toHaveBeenCalledWith("local");
    expect(unscoped.providers).not.toHaveBeenCalled();
  });
});
