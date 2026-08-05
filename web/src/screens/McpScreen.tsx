import { useCallback, useEffect, useState } from "react";

import { ApiError, type Api } from "../api/client";
import type { McpServer, McpTest } from "../api/types";
import { counted } from "../model/plural";
import { parsePaste, type ParsedServer } from "../model/mcpPaste";
import { MCP_PRESETS } from "../model/mcpPresets";
import {
  MCP_RISK_CONSEQUENCE,
  RISK_LEVELS,
  type RiskLevel,
} from "../model/risk";
import "./SettingsScreen.css";
import "./McpScreen.css";

/** Вкладка MCP: подключённые серверы из svarog.yaml + добавление новых с
    реальной проверкой подключения (сервер запускается, делается discovery,
    показывается список инструментов). */
export function McpScreen({ api }: { api: Api }) {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [paste, setPaste] = useState("");
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [override, setOverride] = useState<Partial<ParsedServer> | null>(null);
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
      setTest(null);
      setForcing(false);
      reload();
    } catch (exc: unknown) {
      setStatus(
        exc instanceof ApiError ? exc.message : "Не удалось сохранить сервер.",
      );
    }
  };

  const remove = async (target: string) => {
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
        {servers.length === 0 && (
          <p className="field__help">Пока не подключено ни одного сервера.</p>
        )}
        {servers.map((server) => (
          <div key={server.name} className="secret">
            <span>
              {server.name}
              <span className="mcp__risk"> · {server.risk}</span>
            </span>
            <span className="secret__state mcp__command">
              {[server.command, ...server.args].join(" ")}
            </span>
            <button
              type="button"
              className="btn mcp__remove"
              aria-label={`Удалить ${server.name}`}
              onClick={() => void remove(server.name)}
            >
              Удалить
            </button>
          </div>
        ))}

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
                value={draft.args.join(" ")}
                onChange={(e) =>
                  editField({
                    args: e.target.value.split(/\s+/).filter(Boolean),
                  })
                }
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
