import react from "@vitejs/plugin-react";
// defineConfig из vitest/config — то же, что из vite, но знает про поле test.
import { defineConfig } from "vitest/config";

const GATEWAY = "http://127.0.0.1:8000";

/**
 * Режим раздельной разработки: API живёт на gateway, а не на dev-сервере.
 * Список сверен с web/src/api/client.ts и обязан покрывать каждый префикс
 * оттуда: непроксированный маршрут не падает заметно, а отдаёт index.html
 * dev-сервера, и экран молча получает HTML вместо JSON.
 * У /runs и /sessions дополнительно ws: true — по ним идут потоки событий
 * (/runs/{id}/events и /sessions/events).
 */
const proxy = {
  ...Object.fromEntries(
    [
      "/approvals",
      "/commands",
      "/config",
      "/executors",
      "/fs",
      "/mcp",
      "/memory",
      "/models",
      "/sandboxes",
      "/secrets",
      "/skills",
    ].map((path) => [path, GATEWAY]),
  ),
  "/runs": { target: GATEWAY, ws: true },
  "/sessions": { target: GATEWAY, ws: true },
};

export default defineConfig({
  plugins: [react()],
  // Бандл раздаётся тем же svarog serve с того же origin — базовый путь корневой.
  base: "/",
  server: { proxy },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/setupTests.ts"],
  },
});
