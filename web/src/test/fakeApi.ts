import { vi } from "vitest";

import type { Api } from "../api/client";

/**
 * Полный стенд Api для тестов.
 *
 * Один на все файлы: иначе добавление метода в Api ломает каждый тест по
 * отдельности, и правка превращается в двенадцать одинаковых заплаток.
 */
export function fakeApi(over: Partial<Api> = {}): Api {
  return {
    listSessions: vi.fn().mockResolvedValue([]),
    sessionThread: vi
      .fn()
      .mockResolvedValue({ session_id: "", title: "", items: [] }),
    createSession: vi.fn().mockResolvedValue({ session_id: "new" }),
    deleteSession: vi.fn().mockResolvedValue(undefined),
    sendMessage: vi.fn().mockResolvedValue({ run_id: "r1", state: "running" }),
    decideApproval: vi
      .fn()
      .mockResolvedValue({ run_id: "r1", state: "running" }),
    config: vi.fn().mockResolvedValue({ path: "", sections: [] }),
    previewConfig: vi.fn().mockResolvedValue({
      path: "",
      lines: [],
      changes: 0,
      restart_required: false,
    }),
    saveConfig: vi.fn().mockResolvedValue({
      path: "",
      lines: [],
      changes: 0,
      restart_required: false,
    }),
    secrets: vi.fn().mockResolvedValue([]),
    memoryTree: vi.fn().mockResolvedValue([]),
    memoryFile: vi.fn().mockResolvedValue({
      path: "",
      text: "",
      size_bytes: 0,
      modified_at: "2026-07-27T10:00:00Z",
    }),
    memorySearch: vi.fn().mockResolvedValue([]),
    skills: vi.fn().mockResolvedValue([]),
    runs: vi.fn().mockResolvedValue([]),
    run: vi.fn(),
    runDiff: vi.fn().mockResolvedValue({
      run_id: "",
      committed: "",
      uncommitted: "",
    }),
    providers: vi.fn().mockResolvedValue([]),
    providerModels: vi.fn().mockResolvedValue([]),
    executors: vi.fn().mockResolvedValue([]),
    sandboxes: vi.fn().mockResolvedValue([]),
    commands: vi.fn().mockResolvedValue([]),
    sessionFiles: vi.fn().mockResolvedValue([]),
    uploadAttachment: vi.fn().mockResolvedValue({
      path: ".attachments/stub.png",
      name: "stub.png",
      size_bytes: 0,
      mime: null,
      too_large_for_vision: false,
    }),
    fs: vi
      .fn()
      .mockResolvedValue({ path: "/home/u", parent: "/home", entries: [] }),
    fsRecent: vi.fn().mockResolvedValue([]),
    // Эхо пути: confirm пикера создаёт чат по inspect.path — фиксированное
    // значение в моке подменяло бы выбранную папку в каждом тесте.
    fsInspect: vi
      .fn()
      .mockImplementation((path: string) =>
        Promise.resolve({ path, overlap_warnings: [], blocking: false }),
      ),
    withRoot: vi.fn().mockReturnThis(),
    ...over,
  };
}
