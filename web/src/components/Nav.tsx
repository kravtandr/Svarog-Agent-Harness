import type { SessionSummary } from '../api/types'
import './Nav.css'

const HOUR = 60 * 60 * 1000
const DAY = 24 * HOUR

/** 0 — идёт сейчас, дальше остывает до 4 (архив). */
export function heatLevel(session: SessionSummary, now: number = Date.now()): number {
  if (session.last_state === 'running') return 0
  const age = now - Date.parse(session.updated_at)
  if (age < HOUR) return 1
  if (age < DAY) return 2
  if (age < 7 * DAY) return 3
  return 4
}

function dayLabel(session: SessionSummary, now: number = Date.now()): string {
  const age = now - Date.parse(session.updated_at)
  if (age < DAY) return 'Сегодня'
  if (age < 2 * DAY) return 'Вчера'
  if (age < 7 * DAY) return 'Прошлая неделя'
  return 'Ранее'
}

export function Nav({
  sessions,
  activeId,
  onPick,
  onNew,
}: {
  sessions: SessionSummary[]
  activeId: string | null
  onPick: (sessionId: string) => void
  onNew: () => void
}) {
  let lastLabel = ''

  return (
    <nav className="nav">
      <div className="nav__top">Сварог</div>
      <button type="button" className="nav__new" onClick={onNew}>
        ＋ Новый чат
      </button>

      <div className="nav__list">
        {sessions.map((session) => {
          const label = dayLabel(session)
          const header = label === lastLabel ? null : <div className="nav__day">{label}</div>
          lastLabel = label
          return (
            <div key={session.session_id}>
              {header}
              <button
                type="button"
                className={`nav__item${session.session_id === activeId ? ' nav__item--active' : ''}`}
                onClick={() => onPick(session.session_id)}
              >
                <span
                  className="heat"
                  data-testid={`heat-${session.session_id}`}
                  data-heat={heatLevel(session)}
                />
                <span className="nav__title">{session.title}</span>
              </button>
            </div>
          )
        })}
      </div>

      <div className="nav__foot">
        {/* Экраны разделов ещё не сделаны. Пока их нет, кнопки выключены:
            интерфейс не должен обещать переход, которого не произойдёт. */}
        {['Скиллы', 'Память', 'Настройки'].map((section) => (
          <button
            key={section}
            type="button"
            className="nav__section"
            title="Появится в следующем шаге"
            disabled
          >
            {section}
          </button>
        ))}
      </div>
    </nav>
  )
}
