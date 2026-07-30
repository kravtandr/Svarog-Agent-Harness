import { useEffect, useRef, useState } from "react";

import { type Api } from "../api/client";
import type { FsListing, RecentRoot, RootInspect } from "../api/types";
import {
  Completion,
  COMPLETION_LISTBOX_ID,
  type CompletionItem,
} from "./Completion";
import "./WorkspacePicker.css";

/**
 * Экран выбора рабочей папки нового чата (спека 2026-07-30).
 *
 * Три механики пишут в одно состояние-«кандидат»: ввод с автодополнением,
 * недавние корни и колоночный обзор ФС. Подтверждение — onPick(path);
 * отклонённый promise (422 сервера) рисуется инлайн, экран не закрывается.
 */
export function WorkspacePicker({
  api,
  onPick,
  onCancel,
}: {
  api: Api;
  onPick: (path: string, acceptOverlap?: boolean) => Promise<void>;
  onCancel: () => void;
}) {
  const [value, setValue] = useState("");
  const [suggestions, setSuggestions] = useState<CompletionItem[]>([]);
  const [active, setActive] = useState(0);
  const [recents, setRecents] = useState<RecentRoot[]>([]);
  const [listing, setListing] = useState<FsListing | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Папка, где control-plane пересекается с workspace (ADR-0015 §0.3):
  // вместо создания чата показываем диалог «принять риски» (ADR-0018).
  const [overlap, setOverlap] = useState<RootInspect | null>(null);

  useEffect(() => {
    api
      .fsRecent()
      .then(setRecents)
      .catch(() => setRecents([]));
    // Обзор стартует с $HOME: сервер трактует отсутствие path как домашний каталог.
    api
      .fs()
      .then(setListing)
      .catch(() => setListing(null));
  }, [api]);

  // Автодополнение: каталог до последнего «/», фильтр по остатку-префиксу.
  const timer = useRef<number>(0);
  useEffect(() => {
    window.clearTimeout(timer.current);
    const cut = value.lastIndexOf("/");
    if (cut < 0) {
      setSuggestions([]);
      return;
    }
    timer.current = window.setTimeout(() => {
      const dir = value.slice(0, cut) || "/";
      const prefix = value.slice(cut + 1).toLowerCase();
      api
        .fs(dir)
        .then((found) => {
          setSuggestions(
            found.entries
              .filter(
                (entry) =>
                  entry.accessible &&
                  entry.name.toLowerCase().startsWith(prefix),
              )
              .slice(0, 8)
              .map((entry) => ({
                value: entry.path,
                label: entry.name,
                description: entry.path,
              })),
          );
          setActive(0);
        })
        .catch(() => setSuggestions([]));
    }, 150);
    return () => window.clearTimeout(timer.current);
  }, [api, value]);

  const create = (path: string, acceptOverlap?: boolean) => {
    setError(null);
    const picked = acceptOverlap ? onPick(path, true) : onPick(path);
    picked.catch((exc: unknown) => {
      setError(exc instanceof Error ? exc.message : "Не удалось создать чат.");
    });
  };

  const confirm = (path: string) => {
    setError(null);
    // Проверка папки до создания: пересечение с control-plane показывается
    // диалогом здесь, а не страшной 422-й на первом сообщении.
    api
      .fsInspect(path)
      .then((inspect) => {
        if (inspect.blocking) setOverlap(inspect);
        else create(inspect.path);
      })
      .catch((exc: unknown) => {
        setError(
          exc instanceof Error ? exc.message : "Не удалось проверить папку.",
        );
      });
  };

  const browseTo = (path: string) => {
    setError(null);
    api
      .fs(path)
      .then(setListing)
      .catch((exc: unknown) => {
        setError(
          exc instanceof Error ? exc.message : "Не удалось открыть папку.",
        );
      });
  };

  // Хлебные крошки: /home/u → ["/", "/home", "/home/u"].
  const crumbs =
    listing === null
      ? []
      : listing.path
          .split("/")
          .filter(Boolean)
          .reduce<{ name: string; path: string }[]>(
            (acc, name) => [
              ...acc,
              { name, path: `${acc[acc.length - 1]?.path ?? ""}/${name}` },
            ],
            [],
          );

  if (overlap !== null) {
    // Диалог согласия вместо остального пикера: решение одно и осознанное,
    // отвлекающих элементов рядом быть не должно.
    return (
      <div className="workspace-picker">
        <section
          className="workspace-picker__overlap"
          role="alertdialog"
          aria-label="В этой папке живут данные Сварога"
        >
          <h2 className="workspace-picker__title">
            В этой папке живут данные Сварога
          </h2>
          <p className="workspace-picker__overlap-text">
            База, память или скиллы Сварога лежат внутри выбранной папки —
            агент, работая здесь, сможет их читать и менять. Это риск для
            целостности его собственных данных. Можно продолжить, приняв риск, —
            все возможности сохранятся.
          </p>
          <details className="workspace-picker__overlap-details">
            <summary>Что именно пересекается</summary>
            <ul>
              {overlap.overlap_warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </details>
          <footer className="workspace-picker__actions">
            <button
              type="button"
              className="workspace-picker__confirm"
              onClick={() => create(overlap.path, true)}
            >
              Принять риски и продолжить
            </button>
            <button
              type="button"
              className="workspace-picker__cancel"
              onClick={() => setOverlap(null)}
            >
              Выбрать другую папку
            </button>
          </footer>
          {error !== null && <p className="workspace-picker__error">{error}</p>}
        </section>
      </div>
    );
  }

  return (
    <div className="workspace-picker">
      <h2 className="workspace-picker__title">Где работать?</h2>

      <div className="workspace-picker__field">
        <input
          role="combobox"
          aria-label="Путь к папке"
          aria-expanded={suggestions.length > 0}
          aria-controls={COMPLETION_LISTBOX_ID}
          aria-activedescendant={
            suggestions.length > 0 ? `completion-option-${active}` : undefined
          }
          className="workspace-picker__input"
          placeholder="/путь/к/проекту"
          value={value}
          autoFocus
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown" && suggestions.length > 0) {
              event.preventDefault();
              setActive((index) => (index + 1) % suggestions.length);
            } else if (event.key === "ArrowUp" && suggestions.length > 0) {
              event.preventDefault();
              setActive(
                (index) =>
                  (index - 1 + suggestions.length) % suggestions.length,
              );
            } else if (event.key === "Enter" && value.trim() !== "") {
              event.preventDefault();
              // С открытыми подсказками Enter берёт активную, иначе — ввод.
              confirm(
                suggestions.length > 0
                  ? suggestions[active].value
                  : value.trim(),
              );
            } else if (event.key === "Escape") {
              setSuggestions([]);
            }
          }}
        />
        <Completion
          items={suggestions}
          active={active}
          onPick={(picked) => {
            setValue(picked);
            setSuggestions([]);
          }}
        />
      </div>

      {error !== null && <p className="workspace-picker__error">{error}</p>}

      {recents.length > 0 && (
        <section className="workspace-picker__recents">
          <h3 className="workspace-picker__heading">Недавние</h3>
          {recents.map((recent) => (
            <button
              key={recent.path}
              type="button"
              className="workspace-picker__recent"
              disabled={!recent.exists}
              title={recent.exists ? recent.path : "Папка не существует"}
              onClick={() => confirm(recent.path)}
            >
              {recent.path}
            </button>
          ))}
        </section>
      )}

      {listing !== null && (
        <section className="workspace-picker__browser">
          <h3 className="workspace-picker__heading">Обзор</h3>
          <nav className="workspace-picker__crumbs" aria-label="Путь">
            <button type="button" onClick={() => browseTo("/")}>
              /
            </button>
            {crumbs.map((crumb) => (
              <button
                key={crumb.path}
                type="button"
                onClick={() => browseTo(crumb.path)}
              >
                {crumb.name}
              </button>
            ))}
          </nav>
          <ul className="workspace-picker__dirs">
            {listing.entries.map((entry) => (
              <li key={entry.path}>
                <button
                  type="button"
                  disabled={!entry.accessible}
                  onClick={() => browseTo(entry.path)}
                >
                  {entry.name}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <footer className="workspace-picker__actions">
        <button
          type="button"
          className="workspace-picker__confirm"
          disabled={listing === null}
          onClick={() => listing !== null && confirm(listing.path)}
        >
          Выбрать эту папку
        </button>
        <button
          type="button"
          className="workspace-picker__cancel"
          onClick={onCancel}
        >
          Отмена
        </button>
      </footer>
    </div>
  );
}
