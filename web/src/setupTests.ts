import "@testing-library/jest-dom/vitest";

import { beforeEach } from "vitest";

// jsdom не реализует URL.createObjectURL/revokeObjectURL. ChatScreen рисует
// миниатюры вложений через blob-URL (fetch с Authorization, а не голый
// <img src>, который не может послать токен) — без этой заглушки любой
// тест, где в ленте есть вложение, падал бы на вызове несуществующего
// метода, а не на содержательной проверке.
if (typeof URL.createObjectURL !== "function") {
  URL.createObjectURL = () => "blob:mock";
}
if (typeof URL.revokeObjectURL !== "function") {
  URL.revokeObjectURL = () => {};
}

// jsdom этой конфигурации не даёт window.localStorage. В продакшене
// composerPrefs молча работает «без памяти» (try/catch), но тестам нужна
// настоящая запись/чтение — простая in-memory замена.
if (window.localStorage === undefined) {
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => {
        store.set(key, String(value));
      },
      removeItem: (key: string) => {
        store.delete(key);
      },
      clear: () => {
        store.clear();
      },
      key: (index: number) => [...store.keys()][index] ?? null,
      get length() {
        return store.size;
      },
    },
  });
}

// Сохранённый выбор композера (composerPrefs) не должен протекать между
// тестами: любое взаимодействие с селектами теперь пишет в localStorage.
beforeEach(() => {
  window.localStorage.clear();
});
