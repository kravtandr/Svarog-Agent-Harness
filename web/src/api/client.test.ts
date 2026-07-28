import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, createClient } from "./client";

describe("клиент API", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("подставляет bearer и базовый URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("[]", {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const api = createClient({
      baseUrl: "http://svarog.test",
      token: "секрет",
    });
    await api.listSessions();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://svarog.test/sessions");
    expect((init.headers as Record<string, string>).Authorization).toBe(
      "Bearer секрет",
    );
  });

  it("не шлёт заголовок авторизации без токена", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await createClient({ baseUrl: "" }).listSessions();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(
      (init.headers as Record<string, string>).Authorization,
    ).toBeUndefined();
  });

  it("превращает ошибку сервера в ApiError с текстом detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "нет такой сессии" }), {
          status: 404,
        }),
      ),
    );

    const api = createClient({ baseUrl: "" });

    await expect(api.sessionThread("нет")).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      message: "нет такой сессии",
    });
    await expect(api.sessionThread("нет")).rejects.toBeInstanceOf(ApiError);
  });

  it("экранирует идентификатор сессии в пути", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await createClient({ baseUrl: "" }).sendMessage("a/b", "привет");

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("/sessions/a%2Fb/messages");
  });

  it("передаёт override сообщения и опускает пустые поля", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ run_id: "r1", state: "running" }), {
        status: 201,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const api = createClient({ baseUrl: "" });

    await api.sendMessage("s1", "привет", "yolo", {
      executor: "external",
      model: "x/y",
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body).toEqual({
      text: "привет",
      autonomy: "yolo",
      executor: "external",
      model: "x/y",
    });
  });

  it("кладёт adapter override в тело запроса", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ run_id: "r1", state: "running" }), {
        status: 201,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const api = createClient({ baseUrl: "" });

    await api.sendMessage("s1", "привет", "yolo", {
      executor: "external",
      adapter: "opencode",
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body).toEqual({
      text: "привет",
      autonomy: "yolo",
      executor: "external",
      adapter: "opencode",
    });
  });

  it("запрашивает список провайдеров", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify([
          {
            name: "openrouter",
            base_url: "https://x",
            model: "m",
            is_default: true,
          },
        ]),
        { status: 200 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await createClient({ baseUrl: "" }).providers();

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("/models");
    expect(result).toEqual([
      {
        name: "openrouter",
        base_url: "https://x",
        model: "m",
        is_default: true,
      },
    ]);
  });

  it("экранирует имя провайдера при запросе его каталога моделей", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify([
          {
            id: "gpt-x",
            name: null,
            context_length: null,
            input_usd_per_mtok: null,
            output_usd_per_mtok: null,
          },
        ]),
        { status: 200 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await createClient({ baseUrl: "" }).providerModels("a/b");

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("/models/a%2Fb");
    expect(result).toEqual([
      {
        id: "gpt-x",
        name: null,
        context_length: null,
        input_usd_per_mtok: null,
        output_usd_per_mtok: null,
      },
    ]);
  });

  it("загружает вложение как multipart и возвращает путь", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          path: ".attachments/ab12_скрин.png",
          name: "скрин.png",
          size_bytes: 4,
          mime: "image/png",
          too_large_for_vision: false,
        }),
        { status: 201, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const api = createClient({ baseUrl: "" });

    const file = new File([new Uint8Array([1, 2, 3, 4])], "скрин.png", {
      type: "image/png",
    });
    const stored = await api.uploadAttachment("s1", file);

    expect(stored.path).toBe(".attachments/ab12_скрин.png");
    const init = fetchMock.mock.calls[0][1];
    expect(init.body).toBeInstanceOf(FormData);
    expect(init.headers?.["content-type"]).toBeUndefined();
  });

  it("передаёт пути вложений вместе с текстом", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ run_id: "r1", state: "running" }), {
        status: 201,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const api = createClient({ baseUrl: "" });

    await api.sendMessage("s1", "смотри", "yolo", {}, [
      ".attachments/a_скрин.png",
    ]);

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body).toEqual({
      text: "смотри",
      autonomy: "yolo",
      attachments: [".attachments/a_скрин.png"],
    });
  });

  it("не отправляет attachments, если список пуст", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ run_id: "r1", state: "running" }), {
        status: 201,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const api = createClient({ baseUrl: "" });

    await api.sendMessage("s1", "смотри", "yolo", {}, []);

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body).toEqual({ text: "смотри", autonomy: "yolo" });
  });
});
