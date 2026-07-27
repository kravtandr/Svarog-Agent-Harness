import react from "@vitejs/plugin-react";
// defineConfig из vitest/config — то же, что из vite, но знает про поле test.
import { defineConfig } from "vitest/config";

const GATEWAY = "http://127.0.0.1:8000";

/**
 * Режим раздельной разработки: API живёт на gateway, а не на dev-сервере.
 * Перечислены все маршруты клиента — без /config, /secrets, /memory и
 * /skills соответствующие экраны получали бы HTML dev-сервера вместо JSON.
 * У /runs дополнительно ws: true — по нему идёт поток событий.
 */
const proxy = {
  ...Object.fromEntries(
    ["/sessions", "/approvals", "/config", "/secrets", "/memory", "/skills"].map(
      (path) => [path, GATEWAY],
    ),
  ),
  "/runs": { target: GATEWAY, ws: true },
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
