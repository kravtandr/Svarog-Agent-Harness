import { useCallback, useEffect, useState } from "react";

import { ApiError, type Api, type ConfigValues } from "../api/client";
import { counted } from "../model/plural";
import type {
  ConfigField,
  ConfigView,
  DiffLine,
  ModelCard,
  ProviderCard,
  SecretView,
} from "../api/types";
import "./SettingsScreen.css";

type Pane =
  | { kind: "section"; key: string }
  | { kind: "secrets" }
  | { kind: "providers" }
  | { kind: "executors" };

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
            : counted(changes, "изменение", "изменения", "изменений")}
        </span>
      </div>
    </aside>
  );
}

/** «163840» → «164K»; строке каталога важен порядок величины, не точность. */
function contextLabel(length: number | null): string {
  if (length === null) return "";
  return length >= 1000 ? `${Math.round(length / 1000)}K` : String(length);
}

function priceLabel(card: ModelCard): string {
  if (card.input_usd_per_mtok === null) return "";
  return `$${card.input_usd_per_mtok}/${card.output_usd_per_mtok ?? "?"} за Mtok`;
}

/** Каталог `/models` с фильтром: у OpenRouter и LiteLLM моделей десятки и
    сотни, простой список без поиска нечитаем. Клик отдаёт id наружу. */
function CatalogList({
  cards,
  onPick,
}: {
  cards: ModelCard[];
  onPick: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const shown = cards.filter(
    (card) =>
      needle === "" ||
      card.id.toLowerCase().includes(needle) ||
      (card.name ?? "").toLowerCase().includes(needle),
  );
  return (
    <div className="catalog">
      {cards.length > 8 && (
        <input
          className="field__control"
          aria-label="Фильтр моделей"
          placeholder={`Фильтр — ${counted(cards.length, "модель", "модели", "моделей")}`}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      )}
      <ul className="catalog__list">
        {shown.map((card) => (
          <li key={card.id}>
            <button
              type="button"
              className="catalog__row"
              onClick={() => onPick(card.id)}
            >
              <span className="catalog__name">{card.name ?? card.id}</span>
              <span className="catalog__meta">
                {contextLabel(card.context_length)} {priceLabel(card)}
              </span>
            </button>
          </li>
        ))}
        {shown.length === 0 && <li className="catalog__empty">Пусто.</li>}
      </ul>
    </div>
  );
}

function ProvidersPane({ api }: { api: Api }) {
  const [providers, setProviders] = useState<ProviderCard[]>([]);
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  // Каталоги: развёрнутый сохранённый провайдер + результат скана формы.
  const [openCatalog, setOpenCatalog] = useState<string | null>(null);
  const [catalogs, setCatalogs] = useState<
    Record<string, ModelCard[] | string>
  >({});
  const [scan, setScan] = useState<ModelCard[] | string | null>(null);
  const [scanning, setScanning] = useState(false);

  const reload = useCallback(() => {
    api
      .providers()
      .then(setProviders)
      .catch(() => {});
  }, [api]);
  useEffect(reload, [reload]);

  const submit = async () => {
    setStatus(null);
    try {
      await api.addProvider({
        name: name.trim(),
        base_url: baseUrl.trim(),
        model: model.trim(),
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
      });
      setStatus(`Провайдер «${name.trim()}» сохранён.`);
      setName("");
      setBaseUrl("");
      setModel("");
      setApiKey("");
      setScan(null);
      // Кэш каталогов сброшен — держать секцию развёрнутой нельзя: она
      // застряла бы на «Загружаем…» без повторного запроса.
      setOpenCatalog(null);
      setCatalogs({});
      reload();
    } catch (exc: unknown) {
      setStatus(
        exc instanceof ApiError
          ? exc.message
          : "Не удалось сохранить провайдера.",
      );
    }
  };

  const makeDefault = async (provider: string) => {
    setStatus(null);
    try {
      await api.executorDefaults({ executor: "native", provider });
      setStatus(`Теперь по умолчанию — «${provider}».`);
      reload();
    } catch (exc: unknown) {
      setStatus(
        exc instanceof ApiError
          ? exc.message
          : "Не удалось переключить провайдера.",
      );
    }
  };

  const toggleCatalog = (provider: string) => {
    if (openCatalog === provider) {
      setOpenCatalog(null);
      return;
    }
    setOpenCatalog(provider);
    if (catalogs[provider] !== undefined) return;
    api
      .providerModels(provider)
      .then((cards) =>
        setCatalogs((current) => ({ ...current, [provider]: cards })),
      )
      .catch((exc: unknown) =>
        setCatalogs((current) => ({
          ...current,
          [provider]:
            exc instanceof ApiError ? exc.message : "каталог недоступен",
        })),
      );
  };

  const runScan = async () => {
    setScanning(true);
    setScan(null);
    try {
      setScan(
        await api.scanModels({
          base_url: baseUrl.trim(),
          ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        }),
      );
    } catch (exc: unknown) {
      setScan(
        exc instanceof ApiError ? exc.message : "не удалось получить /models",
      );
    } finally {
      setScanning(false);
    }
  };

  return (
    <>
      <h2 className="settings__title">Провайдеры</h2>
      <p className="field__help">
        OpenAI-совместимые endpoints для native/opencode/codex. Ключ уходит в
        хранилище секретов, в svarog.yaml пишется только ссылка на него.
        Переключить активного — «по умолчанию»; разовый выбор на один запуск
        есть прямо в строке чата.
      </p>
      {providers.map((card) => {
        const catalog = catalogs[card.name];
        return (
          <div key={card.name} className="provider">
            <div className="secret">
              <span>
                {card.name}
                {card.is_default ? " · по умолчанию" : ""}
              </span>
              <span className="secret__state">
                {card.model} · {card.base_url}
              </span>
              <span className="provider__actions">
                {!card.is_default && (
                  <button
                    type="button"
                    className="btn btn--small"
                    onClick={() => void makeDefault(card.name)}
                  >
                    По умолчанию
                  </button>
                )}
                <button
                  type="button"
                  className="btn btn--small"
                  onClick={() => toggleCatalog(card.name)}
                >
                  {openCatalog === card.name ? "Скрыть модели" : "Модели"}
                </button>
              </span>
            </div>
            {openCatalog === card.name &&
              (catalog === undefined ? (
                <p className="field__help">Загружаем каталог…</p>
              ) : typeof catalog === "string" ? (
                <p className="field__error">{catalog}</p>
              ) : (
                <>
                  <p className="field__help">
                    Клик по модели подставит её в форму ниже — «Сохранить
                    провайдера» сделает её моделью «{card.name}».
                  </p>
                  <CatalogList
                    cards={catalog}
                    onPick={(id) => {
                      setName(card.name);
                      setBaseUrl(card.base_url);
                      setModel(id);
                      setStatus(null);
                    }}
                  />
                </>
              ))}
          </div>
        );
      })}
      <h3 className="settings__title">Добавить / обновить</h3>
      <div className="field">
        <label className="field__label" htmlFor="prov-name">
          Имя
        </label>
        <input
          id="prov-name"
          className="field__control"
          value={name}
          placeholder="groq"
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="field__label" htmlFor="prov-url">
          Base URL (с /v1)
        </label>
        <input
          id="prov-url"
          className="field__control"
          value={baseUrl}
          placeholder="https://api.groq.com/openai/v1"
          onChange={(e) => setBaseUrl(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="field__label" htmlFor="prov-key">
          API-ключ (опционально)
        </label>
        <input
          id="prov-key"
          className="field__control"
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="field__label" htmlFor="prov-model">
          Модель по умолчанию
        </label>
        <p className="field__help">
          «Сканировать» спросит у провайдера список /models — клик по модели
          заполнит поле.
        </p>
        <div className="settings__executor-row">
          <input
            id="prov-model"
            className="field__control"
            value={model}
            placeholder="llama-3.3-70b-versatile"
            onChange={(e) => setModel(e.target.value)}
          />
          <button
            type="button"
            className="btn"
            disabled={!baseUrl.trim() || scanning}
            onClick={() => void runScan()}
          >
            {scanning ? "Сканируем…" : "Сканировать"}
          </button>
        </div>
        {typeof scan === "string" && <p className="field__error">{scan}</p>}
        {Array.isArray(scan) && (
          <CatalogList cards={scan} onPick={(id) => setModel(id)} />
        )}
      </div>
      <button
        type="button"
        className="btn"
        disabled={!name.trim() || !baseUrl.trim() || !model.trim()}
        onClick={() => void submit()}
      >
        Сохранить провайдера
      </button>
      {status !== null && <p className="field__help">{status}</p>}
    </>
  );
}

const EXECUTORS: { id: string; title: string; provider: boolean }[] = [
  { id: "native", title: "native", provider: true },
  { id: "opencode", title: "opencode", provider: true },
  { id: "codex", title: "codex", provider: true },
  { id: "claude-code", title: "claude-code", provider: false },
];

function ExecutorsPane({ api }: { api: Api }) {
  const [providers, setProviders] = useState<ProviderCard[]>([]);
  const [drafts, setDrafts] = useState<
    Record<string, { provider: string; model: string }>
  >({});
  const [status, setStatus] = useState<string | null>(null);

  useEffect(() => {
    api
      .providers()
      .then(setProviders)
      .catch(() => {});
  }, [api]);

  const save = async (id: string) => {
    const draft = drafts[id] ?? { provider: "", model: "" };
    setStatus(null);
    try {
      await api.executorDefaults({
        executor: id,
        ...(draft.provider ? { provider: draft.provider } : {}),
        ...(draft.model.trim() ? { model: draft.model.trim() } : {}),
      });
      setStatus(`Дефолты «${id}» сохранены.`);
    } catch (exc: unknown) {
      setStatus(
        exc instanceof ApiError ? exc.message : "Не удалось сохранить дефолты.",
      );
    }
  };

  return (
    <>
      <h2 className="settings__title">Исполнители</h2>
      <p className="field__help">
        Модель по умолчанию для каждого исполнителя; для native и opencode — ещё
        и провайдер (claude-code ходит по своей подписке). Пустые поля не
        меняются.
      </p>
      {EXECUTORS.map((executor) => {
        const draft = drafts[executor.id] ?? { provider: "", model: "" };
        const patch = (part: Partial<{ provider: string; model: string }>) =>
          setDrafts((current) => ({
            ...current,
            [executor.id]: { ...draft, ...part },
          }));
        return (
          <div key={executor.id} className="field">
            <label className="field__label">{executor.title}</label>
            <div className="settings__executor-row">
              {executor.provider && (
                <select
                  className="field__control"
                  aria-label={`Провайдер ${executor.title}`}
                  value={draft.provider}
                  onChange={(e) => patch({ provider: e.target.value })}
                >
                  <option value="">провайдер…</option>
                  {providers.map((card) => (
                    <option key={card.name} value={card.name}>
                      {card.name}
                    </option>
                  ))}
                </select>
              )}
              <input
                className="field__control"
                aria-label={`Модель ${executor.title}`}
                placeholder="модель по умолчанию"
                value={draft.model}
                onChange={(e) => patch({ model: e.target.value })}
              />
              <button
                type="button"
                className="btn"
                disabled={!draft.provider && !draft.model.trim()}
                onClick={() => void save(executor.id)}
              >
                Сохранить
              </button>
            </div>
          </div>
        );
      })}
      {status !== null && <p className="field__help">{status}</p>}
    </>
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
  const [restartRequired, setRestartRequired] = useState(false);

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
    const result = await api.saveConfig(edits);
    setRestartRequired(result.restart_required);
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
            pane.kind === "providers" ? " settings__nav-item--active" : ""
          }`}
          onClick={() => setPane({ kind: "providers" })}
        >
          Провайдеры
        </button>
        <button
          type="button"
          className={`settings__nav-item${
            pane.kind === "executors" ? " settings__nav-item--active" : ""
          }`}
          onClick={() => setPane({ kind: "executors" })}
        >
          Исполнители
        </button>
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
        {pane.kind === "providers" ? (
          <ProvidersPane api={api} />
        ) : pane.kind === "executors" ? (
          <ExecutorsPane api={api} />
        ) : pane.kind === "secrets" ? (
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
              {restartRequired && (
                <p className="settings__notice">
                  Правка сохранена, но вступит в силу только после того, как
                  завершатся текущие запуски.
                </p>
              )}
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
              {`Показать изменения (${changes})`}
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
