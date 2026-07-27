/** Один к одному с pydantic-моделями gateway. Расхождение здесь — ошибка. */

export interface SessionSummary {
  session_id: string
  title: string
  workspace: string | null
  updated_at: string
  runs_count: number
  last_state: string | null
}

export interface ThreadItemView {
  kind: 'user' | 'say' | 'call'
  text: string
  server: string | null
  name: string
  arg: string
  result: string
  status: string
}

export interface SessionThread {
  session_id: string
  title: string
  items: ThreadItemView[]
}

export interface RunRef {
  run_id: string
  state: string
}

/** Значения AutonomyMode сервера (config/schema.py). */
export type Autonomy = 'supervised' | 'auto' | 'yolo'

export const AUTONOMY_LABELS: Record<Autonomy, string> = {
  supervised: 'под надзором',
  auto: 'авто',
  yolo: 'без тормозов',
}
