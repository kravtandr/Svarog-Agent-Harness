import { describe, expect, it } from 'vitest'

import type { ThreadItemView } from '../api/types'
import { applyEvent, fromHistory, type StreamEvent, type ThreadItem } from './thread'

const view = (over: Partial<ThreadItemView>): ThreadItemView => ({
  kind: 'call',
  text: '',
  server: null,
  name: '',
  arg: '',
  result: '',
  status: '',
  ...over,
})

const feed = (events: StreamEvent[]): ThreadItem[] => events.reduce(applyEvent, [] as ThreadItem[])

/** Идентификаторы генерируются на лету — для сравнения формы они не важны. */
const shape = (items: ThreadItem[]) => items.map(({ id: _id, ...rest }) => rest)

describe('нормализация ленты', () => {
  it('переносит историю без потерь', () => {
    const items = fromHistory([
      view({ kind: 'user', text: 'Добавь FTS-поиск' }),
      view({
        name: 'write_file',
        arg: 'memory/index.py',
        result: 'записано 1234 символов',
        status: 'succeeded',
      }),
      view({ kind: 'say', text: 'Готово' }),
    ])

    expect(items.map((item) => item.kind)).toEqual(['user', 'call', 'say'])
    expect(items[1]).toMatchObject({
      kind: 'call',
      name: 'write_file',
      arg: 'memory/index.py',
      result: 'записано 1234 символов',
      status: 'ok',
    })
  })

  it('переводит статусы вызова в три состояния ленты', () => {
    const statuses = ['succeeded', 'running', 'failed', 'denied'].map((status) => {
      const [item] = fromHistory([view({ name: 't', status })])
      return item.kind === 'call' ? item.status : null
    })
    expect(statuses).toEqual(['ok', 'run', 'error', 'error'])
  })

  it('живой поток даёт ту же ленту, что и история', () => {
    const live = feed([
      { type: 'tool_call', tool: 'write_file', arg: 'memory/index.py' },
      {
        type: 'tool_result',
        tool: 'write_file',
        status: 'succeeded',
        result: 'записано 1234 символов',
      },
    ])

    const replayed = fromHistory([
      view({
        name: 'write_file',
        arg: 'memory/index.py',
        result: 'записано 1234 символов',
        status: 'succeeded',
      }),
    ])

    expect(shape(live)).toEqual(shape(replayed))
  })

  it('склеивает text-дельты в одну реплику', () => {
    const items = feed([
      { type: 'text', delta: 'Точный проход ' },
      { type: 'text', delta: 'идёт первым.' },
    ])

    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({ kind: 'say', text: 'Точный проход идёт первым.' })
  })

  it('отделяет MCP-сервер от имени инструмента', () => {
    const items = feed([{ type: 'tool_call', tool: 'github/list_issues', arg: 'label: memory' }])
    expect(items[0]).toMatchObject({ server: 'github', name: 'list_issues' })
  })

  it('добавляет гейт по событию approval_required', () => {
    const items = feed([
      {
        type: 'approval_required',
        approval_id: 'ap-1',
        action_type: 'run_shell',
        payload: { command: 'uv run pytest -q' },
      },
    ])
    expect(items[0]).toMatchObject({
      kind: 'gate',
      approvalId: 'ap-1',
      actionType: 'run_shell',
      command: 'uv run pytest -q',
    })
  })

  it('дописывает результат в последний незавершённый вызов, а не заводит второй', () => {
    const items = feed([
      { type: 'tool_call', tool: 'write_file', arg: 'a.py' },
      { type: 'tool_call', tool: 'write_file', arg: 'b.py' },
      { type: 'tool_result', tool: 'write_file', status: 'succeeded', result: 'записано' },
    ])

    expect(items).toHaveLength(2)
    expect(items[0]).toMatchObject({ arg: 'a.py', status: 'ok', result: 'записано' })
    expect(items[1]).toMatchObject({ arg: 'b.py', status: 'run' })
  })

  it('пропускает события, которых лента не показывает', () => {
    const items = feed([
      { type: 'check', name: 'ruff', status: 'passed' },
      { type: 'commit', sha: 'dfbd62b', branch: 'feat/x' },
      { type: 'run_finished', state: 'completed' },
    ])
    expect(items).toEqual([])
  })
})
