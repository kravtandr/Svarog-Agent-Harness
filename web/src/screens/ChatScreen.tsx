import { useCallback, useEffect, useRef, useState } from 'react'

import type { Api } from '../api/client'
import { subscribeRun } from '../api/stream'
import type { SessionSummary } from '../api/types'
import { Composer } from '../components/Composer'
import { Gate } from '../components/Gate'
import { Nav } from '../components/Nav'
import { Shell } from '../components/Shell'
import { ToolCalls } from '../components/ToolCalls'
import { applyEvent, fromHistory, type ThreadItem } from '../model/thread'
import './ChatScreen.css'

type Call = Extract<ThreadItem, { kind: 'call' }>
type Entry = ThreadItem | { kind: 'calls'; id: string; calls: Call[] }

/** Подряд идущие вызовы рисуются одной группой, а не по карточке на каждый. */
function groupItems(items: ThreadItem[]): Entry[] {
  const grouped: Entry[] = []
  for (const item of items) {
    const last = grouped[grouped.length - 1]
    if (item.kind === 'call') {
      if (last !== undefined && last.kind === 'calls') {
        last.calls.push(item)
        continue
      }
      grouped.push({ kind: 'calls', id: `g-${item.id}`, calls: [item] })
      continue
    }
    grouped.push(item)
  }
  return grouped
}

export function ChatScreen({
  api,
  baseUrl = '',
  token,
}: {
  api: Api
  baseUrl?: string
  token?: string
}) {
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [items, setItems] = useState<ThreadItem[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const unsubscribe = useRef<(() => void) | null>(null)

  useEffect(() => {
    api
      .listSessions()
      .then((listed) => {
        setSessions(listed)
        setActiveId((current) => current ?? listed[0]?.session_id ?? null)
      })
      .catch(() => setError('Не удалось загрузить сессии. Проверьте, что svarog serve запущен.'))
      .finally(() => setLoading(false))
  }, [api])

  useEffect(() => {
    if (activeId === null) return
    api
      .sessionThread(activeId)
      .then((thread) => setItems(fromHistory(thread.items)))
      .catch(() => setError('Не удалось загрузить историю этой сессии.'))
  }, [api, activeId])

  useEffect(() => () => unsubscribe.current?.(), [])

  const send = useCallback(
    async (text: string) => {
      if (activeId === null) return
      setItems((current) => [...current, { kind: 'user', id: `u-${current.length}`, text }])
      const ref = await api.sendMessage(activeId, text)
      unsubscribe.current?.()
      unsubscribe.current = subscribeRun(baseUrl, ref.run_id, token, (event) =>
        setItems((current) => applyEvent(current, event)),
      )
    },
    [api, activeId, baseUrl, token],
  )

  const decide = useCallback(
    async (approvalId: string, approved: boolean) => {
      await api.decideApproval(approvalId, approved)
      setItems((current) =>
        current.filter((item) => !(item.kind === 'gate' && item.approvalId === approvalId)),
      )
    },
    [api],
  )

  const startNew = useCallback(async () => {
    const created = await api.createSession('Новый чат')
    setActiveId(created.session_id)
    setItems([])
    setSessions(await api.listSessions())
  }, [api])

  const active = sessions.find((session) => session.session_id === activeId)

  return (
    <Shell
      nav={
        <Nav
          sessions={sessions}
          activeId={activeId}
          onPick={setActiveId}
          onNew={() => void startNew()}
        />
      }
      bar={<span>{active?.title ?? 'Сварог'}</span>}
    >
      <div className="chat">
        <div className="chat__thread">
          <div className="chat__col">
            {error !== null && <p className="chat__error">{error}</p>}
            {error === null && loading && <p className="chat__hint">Загружаем сессии…</p>}
            {/* Пустой экран — приглашение к действию, а не «нет данных». */}
            {error === null && !loading && items.length === 0 && (
              <p className="chat__hint">
                Поставьте задачу — Сварог заведёт ветку и покажет каждый свой шаг.
              </p>
            )}
            {groupItems(items).map((entry) => {
              if (entry.kind === 'calls') return <ToolCalls key={entry.id} calls={entry.calls} />
              if (entry.kind === 'user')
                return (
                  <div key={entry.id} className="chat__you">
                    {entry.text}
                  </div>
                )
              if (entry.kind === 'say')
                return (
                  <div key={entry.id} className="chat__say">
                    {entry.text}
                  </div>
                )
              if (entry.kind === 'gate')
                return (
                  <Gate
                    key={entry.id}
                    gate={entry}
                    onDecide={(approved) => void decide(entry.approvalId, approved)}
                  />
                )
              return null
            })}
          </div>
        </div>
        <Composer
          onSend={(text) => void send(text)}
          autonomy="под надзором"
          executor="нативный цикл"
          model="qwen3-coder"
        />
      </div>
    </Shell>
  )
}
