import { describe, expect, it } from "vitest";

import type { ThreadItemView } from "../api/types";
import {
  applyEvent,
  fromHistory,
  type StreamEvent,
  type ThreadItem,
} from "./thread";

const view = (over: Partial<ThreadItemView>): ThreadItemView => ({
  kind: "call",
  text: "",
  server: null,
  name: "",
  arg: "",
  result: "",
  status: "",
  ...over,
});

const feed = (events: StreamEvent[]): ThreadItem[] =>
  events.reduce(applyEvent, [] as ThreadItem[]);

/** Идентификаторы генерируются на лету — для сравнения формы они не важны. */
const shape = (items: ThreadItem[]) =>
  items.map(({ id: _id, ...rest }) => rest);

describe("нормализация ленты", () => {
  it("переносит историю без потерь", () => {
    const items = fromHistory([
      view({ kind: "user", text: "Добавь FTS-поиск" }),
      view({
        name: "write_file",
        arg: "memory/index.py",
        result: "записано 1234 символов",
        status: "succeeded",
      }),
      view({ kind: "say", text: "Готово" }),
    ]);

    expect(items.map((item) => item.kind)).toEqual(["user", "call", "say"]);
    expect(items[1]).toMatchObject({
      kind: "call",
      name: "write_file",
      arg: "memory/index.py",
      result: "записано 1234 символов",
      status: "ok",
    });
  });

  it("переводит статусы вызова в три состояния ленты", () => {
    const statuses = ["succeeded", "running", "failed", "denied"].map(
      (status) => {
        const [item] = fromHistory([view({ name: "t", status })]);
        return item.kind === "call" ? item.status : null;
      },
    );
    expect(statuses).toEqual(["ok", "run", "error", "error"]);
  });

  it("живой поток даёт ту же ленту, что и история", () => {
    const live = feed([
      { type: "tool_call", tool: "write_file", arg: "memory/index.py" },
      {
        type: "tool_result",
        tool: "write_file",
        status: "succeeded",
        result: "записано 1234 символов",
      },
    ]);

    const replayed = fromHistory([
      view({
        name: "write_file",
        arg: "memory/index.py",
        result: "записано 1234 символов",
        status: "succeeded",
      }),
    ]);

    expect(shape(live)).toEqual(shape(replayed));
  });

  it("склеивает text-дельты в одну реплику", () => {
    const items = feed([
      { type: "text", delta: "Точный проход " },
      { type: "text", delta: "идёт первым." },
    ]);

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: "say",
      text: "Точный проход идёт первым.",
    });
  });

  it("отделяет MCP-сервер от имени инструмента", () => {
    const items = feed([
      { type: "tool_call", tool: "github/list_issues", arg: "label: memory" },
    ]);
    expect(items[0]).toMatchObject({ server: "github", name: "list_issues" });
  });

  it("добавляет гейт по событию approval_required", () => {
    const items = feed([
      {
        type: "approval_required",
        approval_id: "ap-1",
        action_type: "run_shell",
        payload: { command: "uv run pytest -q" },
      },
    ]);
    expect(items[0]).toMatchObject({
      kind: "gate",
      approvalId: "ap-1",
      actionType: "run_shell",
      command: "uv run pytest -q",
    });
  });

  it("дописывает результат в последний незавершённый вызов, а не заводит второй", () => {
    const items = feed([
      { type: "tool_call", tool: "write_file", arg: "a.py" },
      { type: "tool_call", tool: "write_file", arg: "b.py" },
      {
        type: "tool_result",
        tool: "write_file",
        status: "succeeded",
        result: "записано",
      },
    ]);

    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({
      arg: "a.py",
      status: "ok",
      result: "записано",
    });
    expect(items[1]).toMatchObject({ arg: "b.py", status: "run" });
  });

  it("пропускает события, которых лента не показывает", () => {
    const items = feed([
      { type: "check", name: "ruff", status: "passed" },
      { type: "commit", sha: "dfbd62b", branch: "feat/x" },
      { type: "run_finished", state: "completed" },
    ]);
    expect(items).toEqual([]);
  });
});

describe("вложения в истории", () => {
  it("вынимает пути вложений, но не трогает и не укорачивает текст", () => {
    // Спека прямо требует, чтобы строка "Вложения (...)" осталась видна в
    // ленте — человек должен видеть ровно то, что получил агент, без
    // скрытых добавок. attachments — это дополнительное поле для миниатюр,
    // а не замена text.
    const raw =
      "смотри\n\nВложения (прочитай их read_image / read_document): .attachments/ab_скрин.png";
    const [item] = fromHistory([view({ kind: "user", text: raw })]);
    expect(item).toMatchObject({
      kind: "user",
      text: raw,
      attachments: [".attachments/ab_скрин.png"],
    });
  });

  it("несколько вложений разбираются по запятой", () => {
    const [item] = fromHistory([
      view({
        kind: "user",
        text: "вот файлы\n\nВложения (прочитай их read_image / read_document): .attachments/a.png, .attachments/b.pdf",
      }),
    ]);
    expect(item).toMatchObject({
      attachments: [".attachments/a.png", ".attachments/b.pdf"],
    });
  });

  it("сообщение без вложений — пустой список, а не потерянный текст", () => {
    const [item] = fromHistory([view({ kind: "user", text: "просто текст" })]);
    expect(item).toMatchObject({ text: "просто текст", attachments: [] });
  });
});

describe("итог запуска", () => {
  it("показывает провал, а не оставляет ленту в тишине", () => {
    const items = feed([
      { type: "run_finished", state: "failed", error: "NotFoundError: 404" },
    ]);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: "status",
      failed: true,
    });
    expect((items[0] as { text: string }).text).toContain("NotFoundError");
  });

  it("говорит про ожидание решения без пометки провала", () => {
    const items = feed([{ type: "run_finished", state: "waiting_approval" }]);
    expect(items[0]).toMatchObject({ kind: "status", failed: false });
  });

  it("не дублирует финальный ответ, если он пришёл дельтами", () => {
    const items = feed([
      { type: "text", delta: "Готово." },
      { type: "run_finished", state: "completed", final_answer: "Готово." },
    ]);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "say", text: "Готово." });
  });

  it("показывает финальный ответ, если текста в потоке не было", () => {
    const items = feed([
      {
        type: "run_finished",
        state: "completed",
        final_answer: "Файл создан.",
      },
    ]);
    expect(items[0]).toMatchObject({ kind: "say", text: "Файл создан." });
  });
});
