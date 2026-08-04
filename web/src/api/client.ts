import type {
  Attachment,
  Autonomy,
  ConfigDiff,
  ConfigView,
  ExecutorOption,
  FileSuggestion,
  FsListing,
  MemoryFile,
  MemoryHit,
  MemoryPage,
  ModelCard,
  ProviderCard,
  ProviderCheck,
  RecentRoot,
  RootInspect,
  RunDetail,
  RunDiff,
  RunOverride,
  McpServer,
  McpTest,
  RunRef,
  RunSummary,
  SandboxOption,
  SecretView,
  SessionSummary,
  SessionThread,
  SkillCard,
  SlashCommand,
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
  root?: string;
}

export interface Api {
  listSessions(): Promise<SessionSummary[]>;
  sessionThread(sessionId: string): Promise<SessionThread>;
  createSession(
    title: string,
    path?: string,
    acceptOverlap?: boolean,
  ): Promise<{ session_id: string }>;
  deleteSession(sessionId: string): Promise<void>;
  sendMessage(
    sessionId: string,
    text: string,
    autonomy?: Autonomy,
    override?: RunOverride,
    attachments?: string[],
  ): Promise<RunRef>;
  decideApproval(approvalId: string, approved: boolean): Promise<RunRef>;
  executors(): Promise<ExecutorOption[]>;
  sandboxes(): Promise<SandboxOption[]>;
  commands(): Promise<SlashCommand[]>;
  sessionFiles(sessionId: string, q: string): Promise<FileSuggestion[]>;
  uploadAttachment(sessionId: string, file: File): Promise<Attachment>;
  config(): Promise<ConfigView>;
  previewConfig(values: ConfigValues): Promise<ConfigDiff>;
  saveConfig(values: ConfigValues): Promise<ConfigDiff>;
  addProvider(body: {
    name: string;
    base_url: string;
    model: string;
    api_key?: string;
  }): Promise<ConfigDiff>;
  executorDefaults(body: {
    executor: string;
    provider?: string;
    model?: string;
  }): Promise<ConfigDiff>;
  mcpList(): Promise<McpServer[]>;
  mcpTest(body: {
    command: string;
    args: string[];
    env_refs: string[];
  }): Promise<McpTest>;
  mcpAdd(body: {
    name: string;
    command: string;
    args: string[];
    env_refs: string[];
    risk: string;
  }): Promise<ConfigDiff>;
  mcpRemove(name: string): Promise<ConfigDiff>;
  secrets(): Promise<SecretView[]>;
  memoryTree(): Promise<MemoryPage[]>;
  memoryFile(path: string): Promise<MemoryFile>;
  memorySearch(query: string): Promise<MemoryHit[]>;
  skills(): Promise<SkillCard[]>;
  runs(): Promise<RunSummary[]>;
  run(runId: string): Promise<RunDetail>;
  runDiff(runId: string): Promise<RunDiff>;
  providers(): Promise<ProviderCard[]>;
  providerModels(name: string): Promise<ModelCard[]>;
  /** Живая проверка /models провайдера — мимо кэша каталога. */
  providerCheck(name: string): Promise<ProviderCheck>;
  providerRename(name: string, newName: string): Promise<ConfigDiff>;
  providerRemove(name: string): Promise<ConfigDiff>;
  /** Каталог /models по данным формы — до сохранения провайдера. */
  scanModels(body: {
    base_url: string;
    api_key?: string;
  }): Promise<ModelCard[]>;
  fs(path?: string): Promise<FsListing>;
  fsRecent(): Promise<RecentRoot[]>;
  fsInspect(path: string): Promise<RootInspect>;
  /** Копия клиента с X-Svarog-Root: workspace-экраны активной сессии. */
  withRoot(root: string | null): Api;
}

export function createClient({ baseUrl, token, root }: ClientOptions): Api {
  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    // FormData (загрузка вложений) — без content-type: браузер сам
    // проставит его вместе с multipart-boundary, руками это не собрать.
    const isFormData = init.body instanceof FormData;
    const headers: Record<string, string> = isFormData
      ? {}
      : { "content-type": "application/json" };
    if (token) headers.Authorization = `Bearer ${token}`;
    if (root) headers["X-Svarog-Root"] = root;
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

  const api: Api = {
    listSessions: () => request<SessionSummary[]>("/sessions"),
    sessionThread: (sessionId) =>
      request<SessionThread>(
        `/sessions/${encodeURIComponent(sessionId)}/messages`,
      ),
    createSession: (title, path, acceptOverlap) =>
      request<{ session_id: string }>("/sessions", {
        method: "POST",
        body: JSON.stringify({
          title,
          ...(path ? { path } : {}),
          ...(acceptOverlap ? { accept_overlap: true } : {}),
        }),
      }),
    deleteSession: async (sessionId) => {
      // 204 без тела — request<T> ждёт JSON, поэтому отдельным вызовом.
      const headers: Record<string, string> = {};
      if (token) headers.Authorization = `Bearer ${token}`;
      if (root) headers["X-Svarog-Root"] = root;
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
    sendMessage: (sessionId, text, autonomy, override, attachments) =>
      request<RunRef>(`/sessions/${encodeURIComponent(sessionId)}/messages`, {
        method: "POST",
        // Пустые поля не отправляем: сервер трактует отсутствие как
        // «взять из конфига», а null пришлось бы обрабатывать отдельно.
        body: JSON.stringify({
          text,
          ...(autonomy === undefined ? {} : { autonomy }),
          ...(override?.executor ? { executor: override.executor } : {}),
          ...(override?.provider ? { provider: override.provider } : {}),
          ...(override?.model ? { model: override.model } : {}),
          ...(override?.adapter ? { adapter: override.adapter } : {}),
          ...(override?.sandbox ? { sandbox: override.sandbox } : {}),
          ...(attachments?.length ? { attachments } : {}),
        }),
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
    addProvider: (body) =>
      request<ConfigDiff>("/models/providers", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    executorDefaults: (body) =>
      request<ConfigDiff>("/executors/defaults", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    mcpList: () => request<McpServer[]>("/mcp"),
    mcpTest: (body) =>
      request<McpTest>("/mcp/test", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    mcpAdd: (body) =>
      request<ConfigDiff>("/mcp", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    mcpRemove: (name) =>
      request<ConfigDiff>(`/mcp/${encodeURIComponent(name)}`, {
        method: "DELETE",
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
    providers: () => request<ProviderCard[]>("/models"),
    providerModels: (name) =>
      request<ModelCard[]>(`/models/${encodeURIComponent(name)}`),
    providerCheck: (name) =>
      request<ProviderCheck>(
        `/models/providers/${encodeURIComponent(name)}/check`,
        { method: "POST" },
      ),
    providerRename: (name, newName) =>
      request<ConfigDiff>(
        `/models/providers/${encodeURIComponent(name)}/rename`,
        { method: "POST", body: JSON.stringify({ new_name: newName }) },
      ),
    providerRemove: (name) =>
      request<ConfigDiff>(`/models/providers/${encodeURIComponent(name)}`, {
        method: "DELETE",
      }),
    scanModels: (body) =>
      request<ModelCard[]>("/models/scan", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    executors: () => request<ExecutorOption[]>("/executors"),
    sandboxes: () => request<SandboxOption[]>("/sandboxes"),
    commands: () => request<SlashCommand[]>("/commands"),
    sessionFiles: (sessionId, q) =>
      request<FileSuggestion[]>(
        `/sessions/${encodeURIComponent(sessionId)}/files?q=${encodeURIComponent(q)}`,
      ),
    uploadAttachment: (sessionId, file) => {
      const form = new FormData();
      form.append("file", file);
      return request<Attachment>(
        `/sessions/${encodeURIComponent(sessionId)}/attachments`,
        { method: "POST", body: form },
      );
    },
    fs: (path) =>
      request<FsListing>(path ? `/fs?path=${encodeURIComponent(path)}` : "/fs"),
    fsRecent: () => request<RecentRoot[]>("/fs/recent"),
    fsInspect: (path) =>
      request<RootInspect>(`/fs/inspect?path=${encodeURIComponent(path)}`),
    withRoot: (nextRoot) =>
      nextRoot ? createClient({ baseUrl, token, root: nextRoot }) : api,
  };
  return api;
}
