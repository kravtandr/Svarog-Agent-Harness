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
}

export interface SecretView {
  name: string;
  present: boolean;
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
