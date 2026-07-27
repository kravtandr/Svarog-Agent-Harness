import type { ThreadItemView } from '../api/types'

export type CallStatus = 'ok' | 'run' | 'error'

export type ThreadItem =
  | { kind: 'user'; id: string; text: string }
  | { kind: 'say'; id: string; text: string }
  | {
      kind: 'call'
      id: string
      server: string | null
      name: string
      arg: string
      result: string
      status: CallStatus
    }
  | { kind: 'gate'; id: string; approvalId: string; actionType: string; command: string }

export type StreamEvent =
  | { type: 'text'; delta: string }
  | { type: 'tool_call'; tool: string; arg?: string }
  | { type: 'tool_result'; tool: string; status: string; result?: string }
  | {
      type: 'approval_required'
      approval_id: string
      action_type: string
      payload: Record<string, unknown>
    }
  | { type: string; [key: string]: unknown }

let counter = 0
const nextId = () => `i${(counter += 1)}`

/** succeeded → ok, running → run, всё остальное (failed, denied) → error. */
function toStatus(raw: string): CallStatus {
  if (raw === 'running') return 'run'
  if (raw === 'succeeded') return 'ok'
  return 'error'
}

/** `github/list_issues` → сервер и имя; свой инструмент — сервер null. */
function splitTool(tool: string): { server: string | null; name: string } {
  const at = tool.lastIndexOf('/')
  if (at < 0) return { server: null, name: tool }
  return { server: tool.slice(0, at), name: tool.slice(at + 1) }
}

export function fromHistory(items: ThreadItemView[]): ThreadItem[] {
  return items.map((item): ThreadItem => {
    if (item.kind === 'user') return { kind: 'user', id: nextId(), text: item.text }
    if (item.kind === 'say') return { kind: 'say', id: nextId(), text: item.text }
    return {
      kind: 'call',
      id: nextId(),
      server: item.server,
      name: item.name,
      arg: item.arg,
      result: item.result,
      status: toStatus(item.status),
    }
  })
}

export function applyEvent(items: ThreadItem[], event: StreamEvent): ThreadItem[] {
  if (event.type === 'text') {
    const delta = String((event as { delta: string }).delta)
    const last = items[items.length - 1]
    // Дельты одной реплики склеиваются, иначе лента распадается на слова.
    if (last?.kind === 'say') {
      return [...items.slice(0, -1), { ...last, text: last.text + delta }]
    }
    return [...items, { kind: 'say', id: nextId(), text: delta }]
  }

  if (event.type === 'tool_call') {
    const { tool, arg } = event as { tool: string; arg?: string }
    const { server, name } = splitTool(tool)
    return [
      ...items,
      { kind: 'call', id: nextId(), server, name, arg: arg ?? '', result: '', status: 'run' },
    ]
  }

  if (event.type === 'tool_result') {
    const { tool, status, result } = event as { tool: string; status: string; result?: string }
    const { name } = splitTool(tool)
    // Результат приходит отдельным событием — дописывается в самый ранний
    // незавершённый вызов с тем же именем, а не создаёт вторую строку.
    for (let i = 0; i < items.length; i += 1) {
      const item = items[i]
      if (item.kind === 'call' && item.name === name && item.status === 'run') {
        const patched: ThreadItem = { ...item, status: toStatus(status), result: result ?? '' }
        return [...items.slice(0, i), patched, ...items.slice(i + 1)]
      }
    }
    return items
  }

  if (event.type === 'approval_required') {
    const { approval_id, action_type, payload } = event as {
      approval_id: string
      action_type: string
      payload: Record<string, unknown>
    }
    const command = typeof payload?.command === 'string' ? payload.command : ''
    return [
      ...items,
      { kind: 'gate', id: nextId(), approvalId: approval_id, actionType: action_type, command },
    ]
  }

  return items
}
