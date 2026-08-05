import { useState } from "react";

import type { SessionSummary } from "../api/types";
import { AnimatedTitle } from "./AnimatedTitle";
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

/** Хвост пути для бейджа корня: /home/u/proj/test → test. */
export function rootBase(workspace: string | null): string | null {
  if (!workspace) return null;
  const parts = workspace.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? null;
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

/** Секции списка — папки, в которых начаты чаты (решение 04.08.2026,
    вместо дат): ключ — полный путь (одноимённые папки из разных мест не
    склеиваются), порядок — по свежести первого чата (сервер отдаёт список
    по убыванию updated_at), внутри — тот же хронологический порядок. */
type WorkspaceGroup = {
  key: string;
  label: string;
  full: string | null;
  sessions: SessionSummary[];
};

export function groupByWorkspace(sessions: SessionSummary[]): WorkspaceGroup[] {
  const groups: WorkspaceGroup[] = [];
  const byKey = new Map<string, WorkspaceGroup>();
  for (const session of sessions) {
    const key = session.workspace ?? "";
    let group = byKey.get(key);
    if (group === undefined) {
      group = {
        key,
        label: rootBase(session.workspace) ?? "Без папки",
        full: session.workspace,
        sessions: [],
      };
      byKey.set(key, group);
      groups.push(group);
    }
    group.sessions.push(session);
  }
  return groups;
}

/* Свёрнутые папки — в localStorage: набор полных путей. Битый JSON или
   недоступное хранилище (private mode) — просто всё развёрнуто. */
const COLLAPSED_KEY = "svarog.navCollapsed";

function loadCollapsed(): Set<string> {
  try {
    const parsed: unknown = JSON.parse(
      window.localStorage.getItem(COLLAPSED_KEY) ?? "[]",
    );
    return new Set(
      Array.isArray(parsed) ? parsed.filter((k) => typeof k === "string") : [],
    );
  } catch {
    return new Set();
  }
}

function saveCollapsed(keys: Set<string>): void {
  try {
    window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...keys]));
  } catch {
    // Не запишется — свёрнутость просто не переживёт перезагрузку.
  }
}

export type Section =
  "chat" | "runs" | "skills" | "memory" | "mcp" | "settings";

const SECTIONS: { key: Section; title: string }[] = [
  { key: "runs", title: "Запуски" },
  { key: "skills", title: "Скиллы" },
  { key: "memory", title: "Память" },
  { key: "mcp", title: "MCP" },
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
  // Какой чат раскрыл меню «⋯». Удаление — сразу, без подтверждения
  // (решение 2026-07-30): бейдж корня прижимал крестик, и промахи по нему
  // раздражали сильнее, чем риск лишнего клика; меню разводит цели кликов.
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(loadCollapsed);

  const toggleGroup = (key: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      saveCollapsed(next);
      return next;
    });
  };

  return (
    <nav className="nav">
      <div className="nav__top">Сварог</div>
      <button type="button" className="nav__new" onClick={onNew}>
        ＋ Новый чат
      </button>

      <div className="nav__list" data-testid="nav-list">
        {groupByWorkspace(sessions).map((group) => (
          <div key={group.key}>
            <button
              type="button"
              className="nav__group"
              data-testid="nav-group"
              title={group.full ?? undefined}
              aria-expanded={!collapsed.has(group.key)}
              onClick={() => toggleGroup(group.key)}
            >
              <svg
                className="nav__chevron"
                width="10"
                height="10"
                viewBox="0 0 10 10"
                aria-hidden="true"
              >
                <path
                  d="M2 3.5l3 3 3-3"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              {group.label}
            </button>
            {!collapsed.has(group.key) &&
              group.sessions.map((session) => {
                const busy = busyLabel(session);
                return (
                  <div key={session.session_id}>
                    <div
                      className={`nav__row${session.session_id === activeId ? " nav__row--active" : ""}`}
                    >
                      <button
                        type="button"
                        className="nav__item"
                        onClick={() => {
                          setMenuFor(null); // выбор чата закрывает раскрытое меню
                          onPick(session.session_id);
                        }}
                      >
                        <span
                          className="heat"
                          data-testid={`heat-${session.session_id}`}
                          data-heat={heatLevel(session)}
                        />
                        <AnimatedTitle
                          className="nav__title"
                          text={session.title}
                        />
                        {busy !== null && (
                          <span className="nav__busy" title={`Запуск ${busy}`}>
                            {busy}
                          </span>
                        )}
                      </button>
                      <button
                        type="button"
                        className="nav__more"
                        aria-label={`Меню чата «${session.title}»`}
                        aria-haspopup="menu"
                        aria-expanded={menuFor === session.session_id}
                        onClick={() =>
                          setMenuFor((current) =>
                            current === session.session_id
                              ? null
                              : session.session_id,
                          )
                        }
                      >
                        ⋯
                      </button>
                      {menuFor === session.session_id && (
                        <div className="nav__menu" role="menu">
                          <button
                            type="button"
                            role="menuitem"
                            className="nav__menu-item nav__menu-item--danger"
                            onClick={() => {
                              setMenuFor(null);
                              onDelete(session.session_id);
                            }}
                          >
                            Удалить
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
          </div>
        ))}
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
