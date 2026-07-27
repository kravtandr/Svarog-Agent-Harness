import type { SessionSummary } from "../api/types";
import "./Nav.css";

const HOUR = 60 * 60 * 1000;
const DAY = 24 * HOUR;

/**
 * Сервер отдаёт наивное UTC-время (`Session.updated_at` без зоны), а
 * `Date.parse` без зоны трактует строку как локальную: в Москве возраст
 * сессии уезжал на три часа и «шкала накала» врала.
 */
function parseUtc(value: string): number {
  const withZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`;
  return Date.parse(withZone);
}

/** Незавершённые состояния run'а: чат занят, второй запуск получит отказ. */
const BUSY: Record<string, string> = {
  running: "идёт",
  pending: "в очереди",
  waiting_approval: "ждёт решения",
  suspended: "приостановлен",
};

export function busyLabel(session: SessionSummary): string | null {
  return session.last_state === null
    ? null
    : (BUSY[session.last_state] ?? null);
}

/** 0 — идёт сейчас, дальше остывает до 4 (архив). */
export function heatLevel(
  session: SessionSummary,
  now: number = Date.now(),
): number {
  if (session.last_state === "running") return 0;
  const age = now - parseUtc(session.updated_at);
  if (age < HOUR) return 1;
  if (age < DAY) return 2;
  if (age < 7 * DAY) return 3;
  return 4;
}

function dayLabel(session: SessionSummary, now: number = Date.now()): string {
  const age = now - parseUtc(session.updated_at);
  if (age < DAY) return "Сегодня";
  if (age < 2 * DAY) return "Вчера";
  if (age < 7 * DAY) return "Прошлая неделя";
  return "Ранее";
}

export type Section = "chat" | "runs" | "skills" | "memory" | "settings";

const SECTIONS: { key: Section; title: string }[] = [
  { key: "runs", title: "Запуски" },
  { key: "skills", title: "Скиллы" },
  { key: "memory", title: "Память" },
  { key: "settings", title: "Настройки" },
];

export function Nav({
  sessions,
  activeId,
  onPick,
  onNew,
  onDelete,
  section,
  onSection,
}: {
  sessions: SessionSummary[];
  activeId: string | null;
  onPick: (sessionId: string) => void;
  onNew: () => void;
  onDelete: (sessionId: string) => void;
  section: Section;
  onSection: (section: Section) => void;
}) {
  let lastLabel = "";

  return (
    <nav className="nav">
      <div className="nav__top">Сварог</div>
      <button type="button" className="nav__new" onClick={onNew}>
        ＋ Новый чат
      </button>

      <div className="nav__list">
        {sessions.map((session) => {
          const label = dayLabel(session);
          const header =
            label === lastLabel ? null : (
              <div className="nav__day">{label}</div>
            );
          lastLabel = label;
          const busy = busyLabel(session);
          return (
            <div key={session.session_id}>
              {header}
              <div
                className={`nav__row${session.session_id === activeId ? " nav__row--active" : ""}`}
              >
                <button
                  type="button"
                  className="nav__item"
                  onClick={() => onPick(session.session_id)}
                >
                  <span
                    className="heat"
                    data-testid={`heat-${session.session_id}`}
                    data-heat={heatLevel(session)}
                  />
                  <span className="nav__title">{session.title}</span>
                  {busy !== null && (
                    <span className="nav__busy" title={`Запуск ${busy}`}>
                      {busy}
                    </span>
                  )}
                </button>
                <button
                  type="button"
                  className="nav__delete"
                  aria-label={`Удалить чат «${session.title}»`}
                  onClick={() => onDelete(session.session_id)}
                >
                  ×
                </button>
              </div>
            </div>
          );
        })}
      </div>

      <div className="nav__foot">
        {SECTIONS.map((item) => (
          <button
            key={item.key}
            type="button"
            className={`nav__section${section === item.key ? " nav__item--active" : ""}`}
            onClick={() => onSection(item.key)}
          >
            {item.title}
          </button>
        ))}
      </div>
    </nav>
  );
}
