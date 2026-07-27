import type {
  Autonomy,
  ConfigDiff,
  ConfigView,
  MemoryFile,
  MemoryHit,
  MemoryPage,
  RunDetail,
  RunDiff,
  RunRef,
  RunSummary,
  SecretView,
  SessionSummary,
  SessionThread,
  SkillCard,
} from "./types";

/** Значения формы: путь поля → новое значение. */
export type ConfigValues = Record<string, string | number | boolean>;

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export interface ClientOptions {
  baseUrl: string;
  token?: string;
}

export interface Api {
  listSessions(): Promise<SessionSummary[]>;
  sessionThread(sessionId: string): Promise<SessionThread>;
  createSession(title: string): Promise<{ session_id: string }>;
  deleteSession(sessionId: string): Promise<void>;
  sendMessage(
    sessionId: string,
    text: string,
    autonomy?: Autonomy,
  ): Promise<RunRef>;
  decideApproval(approvalId: string, approved: boolean): Promise<RunRef>;
  config(): Promise<ConfigView>;
  previewConfig(values: ConfigValues): Promise<ConfigDiff>;
  saveConfig(values: ConfigValues): Promise<ConfigDiff>;
  secrets(): Promise<SecretView[]>;
  memoryTree(): Promise<MemoryPage[]>;
  memoryFile(path: string): Promise<MemoryFile>;
  memorySearch(query: string): Promise<MemoryHit[]>;
  skills(): Promise<SkillCard[]>;
  runs(): Promise<RunSummary[]>;
  run(runId: string): Promise<RunDetail>;
  runDiff(runId: string): Promise<RunDiff>;
}

export function createClient({ baseUrl, token }: ClientOptions): Api {
  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers: Record<string, string> = {
      "content-type": "application/json",
    };
    if (token) headers.Authorization = `Bearer ${token}`;
    const response = await fetch(`${baseUrl}${path}`, { ...init, headers });
    if (!response.ok) {
      // detail — стандартная форма ошибки FastAPI; без него берём статус.
      const body: unknown = await response.json().catch(() => null);
      const detail =
        body !== null &&
        typeof body === "object" &&
        "detail" in body &&
        typeof body.detail === "string"
          ? body.detail
          : response.statusText;
      throw new ApiError(response.status, detail);
    }
    return (await response.json()) as T;
  }

  return {
    listSessions: () => request<SessionSummary[]>("/sessions"),
    sessionThread: (sessionId) =>
      request<SessionThread>(
        `/sessions/${encodeURIComponent(sessionId)}/messages`,
      ),
    createSession: (title) =>
      request<{ session_id: string }>("/sessions", {
        method: "POST",
        body: JSON.stringify({ title }),
      }),
    deleteSession: async (sessionId) => {
      // 204 без тела — request<T> ждёт JSON, поэтому отдельным вызовом.
      const headers: Record<string, string> = {};
      if (token) headers.Authorization = `Bearer ${token}`;
      const response = await fetch(
        `${baseUrl}/sessions/${encodeURIComponent(sessionId)}`,
        { method: "DELETE", headers },
      );
      if (!response.ok) {
        const body: unknown = await response.json().catch(() => null);
        const detail =
          body !== null &&
          typeof body === "object" &&
          "detail" in body &&
          typeof body.detail === "string"
            ? body.detail
            : response.statusText;
        throw new ApiError(response.status, detail);
      }
    },
    sendMessage: (sessionId, text, autonomy) =>
      request<RunRef>(`/sessions/${encodeURIComponent(sessionId)}/messages`, {
        method: "POST",
        // autonomy опционален: сервер подставит значение из конфига, если не задан.
        body: JSON.stringify(
          autonomy === undefined ? { text } : { text, autonomy },
        ),
      }),
    decideApproval: (approvalId, approved) =>
      request<RunRef>(`/approvals/${encodeURIComponent(approvalId)}`, {
        method: "POST",
        body: JSON.stringify({ approved }),
      }),
    config: () => request<ConfigView>("/config"),
    previewConfig: (values) =>
      request<ConfigDiff>("/config/preview", {
        method: "POST",
        body: JSON.stringify({ values }),
      }),
    saveConfig: (values) =>
      request<ConfigDiff>("/config", {
        method: "POST",
        body: JSON.stringify({ values }),
      }),
    secrets: () => request<SecretView[]>("/secrets"),
    memoryTree: () => request<MemoryPage[]>("/memory/tree"),
    memoryFile: (path) =>
      request<MemoryFile>(`/memory/file?path=${encodeURIComponent(path)}`),
    memorySearch: (query) =>
      request<MemoryHit[]>(`/memory/search?q=${encodeURIComponent(query)}`),
    skills: () => request<SkillCard[]>("/skills"),
    runs: () => request<RunSummary[]>("/runs"),
    run: (runId) => request<RunDetail>(`/runs/${encodeURIComponent(runId)}`),
    runDiff: (runId) =>
      request<RunDiff>(`/runs/${encodeURIComponent(runId)}/diff`),
  };
}
