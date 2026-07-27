import react from '@vitejs/plugin-react'
// defineConfig из vitest/config — то же, что из vite, но знает про поле test.
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react()],
  // Бандл раздаётся тем же svarog serve с того же origin — базовый путь корневой.
  base: '/',
  server: {
    // Режим раздельной разработки: API живёт на gateway, а не на dev-сервере.
    proxy: {
      '/sessions': 'http://127.0.0.1:8000',
      '/runs': { target: 'http://127.0.0.1:8000', ws: true },
      '/approvals': 'http://127.0.0.1:8000',
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/setupTests.ts'],
  },
})
