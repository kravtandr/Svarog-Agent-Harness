import { useCallback, useEffect, useState } from "react";

import { ApiError, type Api, type ConfigValues } from "../api/client";
import type {
  ConfigField,
  ConfigView,
  DiffLine,
  SecretView,
} from "../api/types";
import "./SettingsScreen.css";

type Pane = { kind: "section"; key: string } | { kind: "secrets" };

function Field({
  field,
  value,
  onChange,
}: {
  field: ConfigField;
  value: string | number | boolean;
  onChange: (value: string | number | boolean) => void;
}) {
  const id = `f-${field.path}`;
  const common = { id, className: "field__control" };

  return (
    <div className="field">
      {field.kind === "bool" ? (
        <label className="field__label" htmlFor={id}>
          <input
            {...common}
            className="field__control field__control--check"
            type="checkbox"
            checked={Boolean(value)}
            onChange={(event) => onChange(event.target.checked)}
          />
          {field.label}
        </label>
      ) : (
        <>
          <label className="field__label" htmlFor={id}>
            {field.label}
          </label>
          {field.help !== "" && <p className="field__help">{field.help}</p>}
          {field.kind === "enum" ? (
            <select
              {...common}
              value={String(value)}
              onChange={(e) => onChange(e.target.value)}
            >
              {field.choices.map((choice) => (
                <option key={choice} value={choice}>
                  {choice}
                </option>
              ))}
            </select>
          ) : (
            <input
              {...common}
              type={field.kind === "str" ? "text" : "number"}
              value={String(value)}
              onChange={(event) =>
                onChange(
                  field.kind === "str"
                    ? event.target.value
                    : Number(event.target.value || 0),
                )
              }
            />
          )}
        </>
      )}
      <div className="field__path">{field.path}</div>
    </div>
  );
}

function DiffPane({
  path,
  lines,
  changes,
  error,
  open,
  onSave,
  onReset,
}: {
  path: string;
  lines: DiffLine[];
  changes: number;
  error: string | null;
  open: boolean;
  onSave: () => void;
  onReset: () => void;
}) {
  return (
    <aside className="diffpane" data-open={open} data-testid="diffpane">
      <div className="diffpane__head">
        Будет записано в <span className="diffpane__file">{path}</span>
      </div>
      <pre className="diffpane__body">
        {lines.map((line, index) => {
          // Знак и текст — одна строка: так она копируется целиком и
          // читается как настоящий дифф.
          const sign =
            line.kind === "add" ? "+" : line.kind === "del" ? "-" : " ";
          return (
            <span key={index} className={`diffpane__line--${line.kind}`}>
              {`${sign}${line.text}`}
            </span>
          );
        })}
      </pre>
      <div className="diffpane__foot">
        <button
          type="button"
          className="btn btn--primary"
          onClick={onSave}
          disabled={changes === 0 || error !== null}
        >
          Сохранить
        </button>
        <button type="button" className="btn" onClick={onReset}>
          Отменить
        </button>
        <span className="diffpane__count">
          {error !== null
            ? "изменения не пройдут проверку"
            : `${changes} изменения`}
        </span>
      </div>
    </aside>
  );
}

export function SettingsScreen({ api }: { api: Api }) {
  const [config, setConfig] = useState<ConfigView | null>(null);
  const [pane, setPane] = useState<Pane>({ kind: "section", key: "" });
  const [edits, setEdits] = useState<ConfigValues>({});
  const [diff, setDiff] = useState<DiffLine[]>([]);
  const [changes, setChanges] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [secrets, setSecrets] = useState<SecretView[] | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);

  useEffect(() => {
    api.config().then((view) => {
      setConfig(view);
      setPane({ kind: "section", key: view.sections[0]?.key ?? "" });
    });
  }, [api]);

  useEffect(() => {
    if (pane.kind !== "secrets" || secrets !== null) return;
    api.secrets().then(setSecrets);
  }, [api, pane, secrets]);

  // Дифф пересчитывается сервером: клиент не умеет сериализовать yaml так же,
  // как это сделает запись, — считать его здесь значит врать о результате.
  useEffect(() => {
    if (Object.keys(edits).length === 0) {
      setDiff([]);
      setChanges(0);
      setError(null);
      return;
    }
    let cancelled = false;
    api
      .previewConfig(edits)
      .then((view) => {
        if (cancelled) return;
        setDiff(view.lines);
        setChanges(view.changes);
        setError(null);
      })
      .catch((exc: unknown) => {
        if (cancelled) return;
        setDiff([]);
        setChanges(0);
        setError(
          exc instanceof ApiError
            ? exc.message
            : "Не удалось проверить изменения",
        );
      });
    return () => {
      cancelled = true;
    };
  }, [api, edits]);

  const save = useCallback(async () => {
    await api.saveConfig(edits);
    setEdits({});
    setSheetOpen(false);
    setConfig(await api.config());
  }, [api, edits]);

  if (config === null)
    return <p className="settings__path">Загружаем настройки…</p>;

  const section = config.sections.find(
    (s) => pane.kind === "section" && s.key === pane.key,
  );

  return (
    <div className="settings">
      <nav className="settings__nav">
        {config.sections.map((item) => (
          <button
            key={item.key}
            type="button"
            className={`settings__nav-item${
              pane.kind === "section" && pane.key === item.key
                ? " settings__nav-item--active"
                : ""
            }`}
            onClick={() => setPane({ kind: "section", key: item.key })}
          >
            {item.title}
          </button>
        ))}
        <button
          type="button"
          className={`settings__nav-item${
            pane.kind === "secrets" ? " settings__nav-item--active" : ""
          }`}
          onClick={() => setPane({ kind: "secrets" })}
        >
          Секреты
        </button>
      </nav>

      <div className="settings__body">
        {pane.kind === "secrets" ? (
          <>
            <h2 className="settings__title">Секреты</h2>
            <p className="field__help">
              Значения не показываются и не редактируются в вебе. Задать —
              командой svarog secrets set или переменной окружения.
            </p>
            {(secrets ?? []).map((secret) => (
              <div key={secret.name} className="secret">
                <span>{secret.name}</span>
                <span
                  className={`secret__state${secret.present ? "" : " secret__state--missing"}`}
                >
                  {secret.present ? "задан" : "не задан"}
                </span>
              </div>
            ))}
          </>
        ) : (
          section !== undefined && (
            <>
              <h2 className="settings__title">{section.title}</h2>
              <p className="settings__path">{config.path}</p>
              {section.fields.map((field) => (
                <div key={field.path}>
                  <Field
                    field={field}
                    value={
                      edits[field.path] ??
                      (field.value as string | number | boolean)
                    }
                    onChange={(value) =>
                      setEdits((current) => ({
                        ...current,
                        [field.path]: value,
                      }))
                    }
                  />
                  {error !== null &&
                    error.includes(field.path.split(".")[1]) && (
                      <p className="field__error">{error}</p>
                    )}
                </div>
              ))}
            </>
          )
        )}
      </div>

      {pane.kind === "section" && (
        <>
          <div className="settings__sheet-button">
            <button
              type="button"
              className="btn"
              onClick={() => setSheetOpen(true)}
            >
              Показать изменения ({changes})
            </button>
          </div>
          <DiffPane
            path={config.path}
            lines={diff}
            changes={changes}
            error={error}
            open={sheetOpen}
            onSave={() => void save()}
            onReset={() => {
              setEdits({});
              setSheetOpen(false);
            }}
          />
        </>
      )}
    </div>
  );
}
