import { describe, expect, it } from "vitest";

import { detectCompletion, parseCommand, replaceToken } from "./completion";

describe("detectCompletion", () => {
  it("молчит на обычном тексте", () => {
    expect(detectCompletion("привет как дела")).toEqual({
      mode: "idle",
      token: "",
    });
  });

  it("видит слэш-команду в начале строки", () => {
    expect(detectCompletion("/he")).toEqual({ mode: "slash", token: "/he" });
  });

  it("не считает слэш командой после пробела", () => {
    expect(detectCompletion("текст /he").mode).toBe("idle");
  });

  it("видит @ в середине строки", () => {
    expect(detectCompletion("посмотри @src/a")).toEqual({
      mode: "at",
      token: "@src/a",
    });
  });

  it("@ важнее слэша", () => {
    expect(detectCompletion("/cmd @fi").mode).toBe("at");
  });

  it("пустой ввод — покой", () => {
    expect(detectCompletion("")).toEqual({ mode: "idle", token: "" });
  });

  it("видит голый слэш", () => {
    expect(detectCompletion("/")).toEqual({ mode: "slash", token: "/" });
  });

  it("видит голый @", () => {
    expect(detectCompletion("@")).toEqual({ mode: "at", token: "@" });
  });

  it("не видит слэш-команду со словом после", () => {
    expect(detectCompletion("/help extra")).toEqual({
      mode: "idle",
      token: "",
    });
  });
});

describe("replaceToken", () => {
  it("заменяет токен под курсором и ставит курсор после вставки", () => {
    const result = replaceToken("смотри @sr", 10, "@src/app.tsx");
    expect(result.text).toBe("смотри @src/app.tsx ");
    expect(result.caret).toBe(result.text.length);
  });

  it("сохраняет хвост строки после курсора", () => {
    const result = replaceToken("смотри @sr конец", 10, "@src/app.tsx");
    expect(result.text).toBe("смотри @src/app.tsx  конец");
  });
});

describe("parseCommand", () => {
  it("обычное сообщение — не команда", () => {
    expect(parseCommand("привет, как дела?")).toBeNull();
  });

  it("узнаёт известную команду", () => {
    expect(parseCommand("/new")).toEqual({ name: "new", args: "" });
  });

  it("неизвестная команда возвращает сырое имя, а не null", () => {
    expect(parseCommand("/опечатка")).toEqual({ name: "", args: "/опечатка" });
  });

  it("голый слэш без имени — тоже неизвестная команда", () => {
    expect(parseCommand("/")).toEqual({ name: "", args: "/" });
  });

  // Слэш-режим требует однословного токена (как в detectCompletion) — иначе
  // реальное сообщение вида "/build the new feature" никогда нельзя было бы
  // отправить: любой слэш в начале строки блокировал бы его как "команду".
  it("текст с пробелом после слэша — обычное сообщение, а не команда", () => {
    expect(parseCommand("/build the new feature")).toBeNull();
  });

  it("перенос строки после слэша — тоже обычное сообщение", () => {
    expect(parseCommand("/new\nещё текст")).toBeNull();
  });
});
