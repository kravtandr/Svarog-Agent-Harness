/** Один к одному с pydantic-моделями gateway. Расхождение здесь — ошибка. */

export interface SessionSummary {
  session_id: string;
  title: string;
  workspace: string | null;
  updated_at: string;
  runs_count: number;
  last_state: string | null;
}

export interface ThreadItemView {
  kind: "user" | "say" | "call";
  text: string;
  server: string | null;
  name: string;
  arg: string;
  result: string;
  status: string;
}

export interface SessionThread {
  session_id: string;
  title: string;
  items: ThreadItemView[];
}

export interface RunRef {
  run_id: string;
  state: string;
}

export interface ConfigField {
  path: string;
  label: string;
  help: string;
  kind: "bool" | "int" | "float" | "str" | "enum";
  value: string | number | boolean | null;
  choices: string[];
  minimum: number | null;
  maximum: number | null;
}

export interface ConfigSection {
  key: string;
  title: string;
  fields: ConfigField[];
}

export interface ConfigView {
  path: string;
  sections: ConfigSection[];
}

export interface DiffLine {
  kind: "same" | "add" | "del";
  text: string;
}

export interface ConfigDiff {
  path: string;
  lines: DiffLine[];
  changes: number;
  // true, если правка легла в файл, но применится только после того, как
  // текущие запуски закончатся (снимок конфига под живым run'ом не меняется).
  restart_required: boolean;
}

export interface SecretView {
  name: string;
  present: boolean;
}

export interface SkillCard {
  name: string;
  description: string;
  version: string;
  risk: string;
}

export interface RunSummary {
  run_id: string;
  state: string;
  task: string;
  autonomy: string;
  iterations: number;
  tokens_used: number;
  cost_usd: number;
  error: string | null;
}

export interface ToolCallView {
  tool_name: string;
  risk_level: string | null;
  policy_decision: string | null;
  status: string;
  error: string | null;
}

export interface RunDetail extends RunSummary {
  messages: Record<string, unknown>[];
  tool_calls: ToolCallView[];
  checks: Record<string, unknown>[];
}

export interface RunDiff {
  run_id: string;
  committed: string;
  uncommitted: string;
}

export interface MemoryPage {
  path: string;
  size_bytes: number;
  modified_at: string;
}

export interface MemoryHit {
  path: string;
  snippet: string;
}

export interface MemoryFile {
  path: string;
  text: string;
  size_bytes: number;
  modified_at: string;
}

/** Значения AutonomyMode сервера (config/schema.py). */
export type Autonomy = "supervised" | "auto" | "yolo";

export const AUTONOMY_LABELS: Record<Autonomy, string> = {
  supervised: "под надзором",
  auto: "авто",
  yolo: "без тормозов",
};

/** Значения executor.type сервера (config/schema.py). */
export type ExecutorKind = "native" | "external";

export const EXECUTOR_LABELS: Record<ExecutorKind, string> = {
  native: "нативный цикл",
  external: "внешний агент",
};

/** Выбор в поле ввода: свойство сообщения, конфиг не меняется. */
export interface RunOverride {
  executor?: ExecutorKind;
  provider?: string;
  model?: string;
}

export interface ProviderCard {
  name: string;
  base_url: string;
  model: string;
  is_default: boolean;
}

export interface ModelCard {
  id: string;
  name: string | null;
  context_length: number | null;
  input_usd_per_mtok: number | null;
  output_usd_per_mtok: number | null;
}

/** Одна запись из GET /executors: значение выбора исполнителя в композере. */
export interface ExecutorOption {
  value: string;
  kind: ExecutorKind;
  adapter: string | null;
  available: boolean;
  is_active: boolean;
}

/** Слэш-команда из GET /commands (автодополнение композера). */
export interface SlashCommand {
  name: string;
  usage: string;
  help: string;
}

/** Подсказка файла из GET /sessions/{id}/files для «@file»-автодополнения. */
export interface FileSuggestion {
  path: string;
  label: string;
}

/** Результат загрузки вложения через POST /sessions/{id}/attachments. */
export interface Attachment {
  path: string;
  name: string;
  size_bytes: number;
  mime: string | null;
  too_large_for_vision: boolean;
}
