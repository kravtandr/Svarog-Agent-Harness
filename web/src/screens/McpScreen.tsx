import { useCallback, useEffect, useState } from "react";

import { ApiError, type Api } from "../api/client";
import type { McpServer, McpTest } from "../api/types";
import { counted } from "../model/plural";
import {
  parsePaste,
  shellJoin,
  shellSplit,
  type ParsedServer,
} from "../model/mcpPaste";
import { MCP_PRESETS } from "../model/mcpPresets";
import {
  MCP_RISK_CONSEQUENCE,
  RISK_LEVELS,
  riskClass,
  riskLabel,
  type RiskLevel,
} from "../model/risk";
import "./SettingsScreen.css";
import "./McpScreen.css";

/** Ключ опроса — не просто имя сервера: сервер с тем же именем, но другими
    полями (удалили и пересоздали под тем же именем, пока запрос ещё летел)
    обязан требовать новой проверки, а не получить результат чужого процесса. */
function probeKey(server: McpServer): string {
  return JSON.stringify([
    server.name,
    server.command,
    server.args,
    server.env_refs,
  ]);
}

/** Вкладка MCP: подключённые серверы из svarog.yaml + добавление новых с
    реальной проверкой подключения (сервер запускается, делается discovery,
    показывается список инструментов). */
export function McpScreen({ api }: { api: Api }) {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [paste, setPaste] = useState("");
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [override, setOverride] = useState<Partial<ParsedServer> | null>(null);
  // Текст поля аргументов хранится отдельно от разобранного списка: показывать
  // в нём shellJoin(draft.args) на каждый ввод значит стирать только что
  // набранный пробел (он ещё не начал новый токен) и переставлять кавычки под
  // курсором. null — «поле следует за разбором вставки».
  const [argsText, setArgsText] = useState<string | null>(null);
  const [risk, setRisk] = useState<RiskLevel>("high");
  const [test, setTest] = useState<McpTest | null>(null);
  const [testing, setTesting] = useState(false);
  const [forcing, setForcing] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  const reload = useCallback(() => {
    api
      .mcpList()
      .then(setServers)
      .catch(() => {});
  }, [api]);
  useEffect(reload, [reload]);

  // Ручная правка перекрывает разбор, но не отменяет его: человек мог
  // поправить одно поле из четырёх, и остальные должны остаться живыми.
  const parsed = parsePaste(paste);
  const draft: ParsedServer = {
    name: override?.name ?? parsed?.name ?? "",
    command: override?.command ?? parsed?.command ?? "",
    args: override?.args ?? parsed?.args ?? [],
    envRefs: override?.envRefs ?? parsed?.envRefs ?? [],
  };

  const editPaste = (value: string) => {
    setPaste(value);
    setOverride(null);
    setArgsText(null);
    setTest(null);
    setForcing(false);
  };

  const editField = (part: Partial<ParsedServer>) => {
    setOverride({ ...(override ?? {}), ...part });
    setTest(null);
    setForcing(false);
  };

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    // Новая проверка отменяет прежнее согласие на «всё равно подключить»:
    // оно было выдано под конкретный провал, а не под кнопку вообще.
    setForcing(false);
    try {
      setTest(
        await api.mcpTest({
          command: draft.command,
          args: draft.args,
          env_refs: draft.envRefs,
        }),
      );
    } catch (exc: unknown) {
      setTest({
        ok: false,
        tools: [],
        error: exc instanceof ApiError ? exc.message : "проверка не удалась",
      });
    } finally {
      setTesting(false);
    }
  };

  const save = async () => {
    // Проверка не обязательна, но провалившаяся — повод переспросить: молча
    // записать заведомо нерабочий сервер значит спрятать ошибку до запуска.
    if (test !== null && !test.ok && !forcing) {
      setForcing(true);
      return;
    }
    setStatus(null);
    try {
      await api.mcpAdd({
        name: draft.name,
        command: draft.command,
        args: draft.args,
        env_refs: draft.envRefs,
        risk,
      });
      setStatus(`Сервер «${draft.name}» сохранён в svarog.yaml.`);
      setPaste("");
      setOverride(null);
      setArgsText(null);
      setTest(null);
      setForcing(false);
      reload();
    } catch (exc: unknown) {
      setStatus(
        exc instanceof ApiError ? exc.message : "Не удалось сохранить сервер.",
      );
    }
  };

  // Проверка живости — по клику, а не при открытии вкладки: автоопрос всех
  // серверов означал бы запуск N процессов при каждом заходе.
  const [probes, setProbes] = useState<Record<string, McpTest | "идёт">>({});
  const [confirming, setConfirming] = useState<string | null>(null);

  // Список могли перезагрузить по причине, никак не связанной с этой
  // карточкой: сохранили другой сервер, удалили какой-то другой. Взведённое
  // «Точно удалить?» — согласие на один конкретный клик, оно не должно
  // пережить список: иначе следующий одиночный клик по давно нажатой
  // карточке снёс бы сервер без нового подтверждения, а при повторном
  // использовании того же имени согласие могло бы уехать на другой сервер.
  // Опросы, чей ключ не совпадает ни с одним нынешним сервером (сервер
  // удалён или пересоздан с другими полями), выкидываются тем же эффектом —
  // их больше некому показывать, а оставлять — значит копить утечку.
  useEffect(() => {
    setConfirming(null);
    const validKeys = new Set(servers.map(probeKey));
    setProbes((current) => {
      const next: Record<string, McpTest | "идёт"> = {};
      let changed = false;
      for (const [key, value] of Object.entries(current)) {
        if (validKeys.has(key)) {
          next[key] = value;
        } else {
          changed = true;
        }
      }
      return changed ? next : current;
    });
  }, [servers]);

  const probe = async (server: McpServer) => {
    const key = probeKey(server);
    setProbes((current) => ({ ...current, [key]: "идёт" }));
    try {
      const result = await api.mcpTest({
        command: server.command,
        args: server.args,
        env_refs: server.env_refs,
      });
      // Пока запрос летел, сервер могли удалить и пересоздать под тем же
      // именем с другими полями — тогда ключ уже не совпадает ни с одной
      // карточкой, и результат просто оседает в состоянии, ничего не ломая.
      setProbes((current) => ({ ...current, [key]: result }));
    } catch (exc: unknown) {
      setProbes((current) => ({
        ...current,
        [key]: {
          ok: false,
          tools: [],
          error: exc instanceof ApiError ? exc.message : "проверка не удалась",
        },
      }));
    }
  };

  const remove = async (target: string) => {
    // Двухкликовое подтверждение вместо window.confirm: тестируемо и не
    // блокирует вкладку нативным диалогом (как у провайдеров).
    if (confirming !== target) {
      setConfirming(target);
      return;
    }
    setConfirming(null);
    try {
      await api.mcpRemove(target);
      reload();
    } catch (exc: unknown) {
      setStatus(
        exc instanceof ApiError ? exc.message : "Не удалось удалить сервер.",
      );
    }
  };

  return (
    <div className="settings">
      <div className="settings__body mcp__body">
        <h2 className="settings__title">MCP-серверы</h2>
        <p className="field__help">
          Инструменты серверов проходят Policy Engine: по умолчанию каждый вызов
          требует подтверждения.
        </p>
        {servers.length === 0 ? (
          <p className="field__help">
            Пока не подключено ни одного сервера — выберите готовый ниже или
            вставьте свою команду.
          </p>
        ) : (
          <div className="mcp__grid">
            {servers.map((server) => {
              const probed = probes[probeKey(server)];
              return (
                <div key={server.name} className="mcp__card">
                  <div className="mcp__card-head">
                    {probed !== undefined && probed !== "идёт" && (
                      <span
                        className={`mcp__dot${probed.ok ? "" : " mcp__dot--bad"}`}
                        aria-hidden="true"
                      />
                    )}
                    <span className="mcp__card-name">{server.name}</span>
                    <span
                      className={`mcp__card-risk ${riskClass(server.risk)}`}
                    >
                      {riskLabel(server.risk)}
                    </span>
                  </div>
                  <div className="mcp__command">
                    {[server.command, ...server.args].join(" ")}
                  </div>
                  {server.env_refs.length > 0 && (
                    <div className="mcp__chips">
                      {server.env_refs.map((ref) => (
                        <span key={ref} className="mcp__chip">
                          {ref}
                        </span>
                      ))}
                    </div>
                  )}
                  {probed === "идёт" && (
                    <p className="field__help">Опрашиваем…</p>
                  )}
                  {probed !== undefined &&
                    probed !== "идёт" &&
                    (probed.ok ? (
                      <div className="mcp__chips">
                        {probed.tools.map((tool) => (
                          <span key={tool} className="mcp__chip">
                            {tool}
                          </span>
                        ))}
                      </div>
                    ) : (
                      <p className="field__error">{probed.error}</p>
                    ))}
                  <div className="mcp__card-actions">
                    <button
                      type="button"
                      className="btn btn--small"
                      aria-label={`Инструменты ${server.name}`}
                      // Каждый опрос поднимает настоящий процесс сервера —
                      // ровно поэтому проверка сделана по клику, а не при
                      // открытии вкладки. Повторный клик плодил бы процессы.
                      disabled={probed === "идёт"}
                      onClick={() => void probe(server)}
                    >
                      Инструменты
                    </button>
                    <button
                      type="button"
                      className="btn btn--small"
                      aria-label={
                        confirming === server.name
                          ? "Точно удалить?"
                          : `Удалить ${server.name}`
                      }
                      onClick={() => void remove(server.name)}
                    >
                      {confirming === server.name
                        ? "Точно удалить?"
                        : "Удалить"}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <h3 className="settings__title">Подключить сервер</h3>
        <div className="field">
          <label className="field__label" htmlFor="mcp-paste">
            Команда или JSON
          </label>
          <input
            id="mcp-paste"
            className="field__control"
            value={paste}
            placeholder="uvx mcp-server-fetch"
            onChange={(e) => editPaste(e.target.value)}
          />
        </div>

        {paste.trim() === "" && (
          <div className="mcp__presets">
            {MCP_PRESETS.map((preset) => (
              <button
                key={preset.id}
                type="button"
                className="mcp__preset"
                onClick={() => {
                  editPaste(preset.paste);
                  setRisk(preset.risk);
                }}
              >
                <span className="mcp__preset-title">{preset.title}</span>
                <span className="mcp__preset-hint">{preset.hint}</span>
              </button>
            ))}
          </div>
        )}

        <button
          type="button"
          className="btn btn--small mcp__details-toggle"
          aria-expanded={detailsOpen}
          onClick={() => setDetailsOpen(!detailsOpen)}
        >
          Уточнить
        </button>

        {detailsOpen && (
          <div className="mcp__details">
            <div className="field">
              <label className="field__label" htmlFor="mcp-name">
                Имя
              </label>
              <input
                id="mcp-name"
                className="field__control"
                value={draft.name}
                onChange={(e) => editField({ name: e.target.value })}
              />
            </div>
            <div className="field">
              <label className="field__label" htmlFor="mcp-command">
                Команда
              </label>
              <input
                id="mcp-command"
                className="field__control"
                value={draft.command}
                onChange={(e) => editField({ command: e.target.value })}
              />
            </div>
            <div className="field">
              <label className="field__label" htmlFor="mcp-args">
                Аргументы
              </label>
              <input
                id="mcp-args"
                className="field__control"
                // Тот же токенизатор, что и у вставки: разбирать это поле
                // через split(/\s+/) значило бы ломать при первой правке
                // ровно те пути с пробелами, которые вставка сохранила.
                value={argsText ?? shellJoin(draft.args)}
                onChange={(e) => {
                  setArgsText(e.target.value);
                  editField({ args: shellSplit(e.target.value) });
                }}
              />
            </div>
            <div className="field">
              <label className="field__label" htmlFor="mcp-env">
                Секреты (имена через запятую)
              </label>
              <p className="field__help">
                Только имена. Значения задаются командой svarog secrets set и в
                svarog.yaml не попадают.
              </p>
              <input
                id="mcp-env"
                className="field__control"
                value={draft.envRefs.join(", ")}
                onChange={(e) =>
                  editField({
                    envRefs: e.target.value
                      .split(",")
                      .map((item) => item.trim())
                      .filter(Boolean),
                  })
                }
              />
            </div>
          </div>
        )}

        <fieldset className="mcp__risk-set">
          <legend className="field__label">Риск инструментов</legend>
          <div className="mcp__segments">
            {RISK_LEVELS.map((level) => (
              <label key={level} className="mcp__segment">
                <input
                  type="radio"
                  name="mcp-risk"
                  value={level}
                  checked={risk === level}
                  onChange={() => setRisk(level)}
                />
                <span>{level}</span>
              </label>
            ))}
          </div>
          <p className="field__help">{MCP_RISK_CONSEQUENCE[risk]}</p>
        </fieldset>

        <div className="mcp__actions">
          <button
            type="button"
            className="btn"
            disabled={draft.command === "" || testing}
            onClick={() => void runTest()}
          >
            {testing ? "Проверяем…" : "Проверить"}
          </button>
          <button
            type="button"
            className="btn btn--primary"
            disabled={draft.name === "" || draft.command === ""}
            onClick={() => void save()}
          >
            {forcing ? "Всё равно подключить?" : "Подключить"}
          </button>
        </div>

        {test !== null && (
          <div className="mcp__result" role="status">
            {test.ok ? (
              <>
                <p className="mcp__result-head">
                  Сервер ответил —{" "}
                  {counted(
                    test.tools.length,
                    "инструмент",
                    "инструмента",
                    "инструментов",
                  )}
                </p>
                <div className="mcp__chips">
                  {test.tools.map((tool) => (
                    <span key={tool} className="mcp__chip">
                      {tool}
                    </span>
                  ))}
                </div>
              </>
            ) : (
              <p className="field__error">Не подключился: {test.error}</p>
            )}
          </div>
        )}
        {status !== null && <p className="field__help">{status}</p>}
      </div>
    </div>
  );
}
