import { useCallback, useEffect, useState } from "react";

import { ApiError, type Api } from "../api/client";
import type { McpServer, McpTest } from "../api/types";
import "./SettingsScreen.css";
import "./McpScreen.css";

const RISKS = ["low", "medium", "high", "critical"];

/** Вкладка MCP: подключённые серверы из svarog.yaml + добавление новых с
    реальной проверкой подключения (сервер запускается, делается discovery,
    показывается список инструментов). */
export function McpScreen({ api }: { api: Api }) {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [envRefs, setEnvRefs] = useState("");
  const [risk, setRisk] = useState("high");
  const [test, setTest] = useState<McpTest | null>(null);
  const [testing, setTesting] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  const reload = useCallback(() => {
    api
      .mcpList()
      .then(setServers)
      .catch(() => {});
  }, [api]);
  useEffect(reload, [reload]);

  const parsedArgs = () => args.trim().split(/\s+/).filter(Boolean);
  const parsedEnv = () =>
    envRefs
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    try {
      setTest(
        await api.mcpTest({
          command: command.trim(),
          args: parsedArgs(),
          env_refs: parsedEnv(),
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
    setStatus(null);
    try {
      await api.mcpAdd({
        name: name.trim(),
        command: command.trim(),
        args: parsedArgs(),
        env_refs: parsedEnv(),
        risk,
      });
      setStatus(`Сервер «${name.trim()}» сохранён в svarog.yaml.`);
      setName("");
      setCommand("");
      setArgs("");
      setEnvRefs("");
      setTest(null);
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
          Инструменты серверов проходят Policy Engine как обычные: риск задаёт
          профиль по умолчанию (high — с подтверждением человека).
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
          <label className="field__label" htmlFor="mcp-name">
            Имя
          </label>
          <input
            id="mcp-name"
            className="field__control"
            value={name}
            placeholder="fetch"
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div className="field">
          <label className="field__label" htmlFor="mcp-command">
            Команда (stdio)
          </label>
          <input
            id="mcp-command"
            className="field__control"
            value={command}
            placeholder="uvx"
            onChange={(e) => setCommand(e.target.value)}
          />
        </div>
        <div className="field">
          <label className="field__label" htmlFor="mcp-args">
            Аргументы (через пробел)
          </label>
          <input
            id="mcp-args"
            className="field__control"
            value={args}
            placeholder="mcp-server-fetch"
            onChange={(e) => setArgs(e.target.value)}
          />
        </div>
        <div className="field">
          <label className="field__label" htmlFor="mcp-env">
            Секреты для env (имена через запятую)
          </label>
          <input
            id="mcp-env"
            className="field__control"
            value={envRefs}
            placeholder="GITHUB_TOKEN"
            onChange={(e) => setEnvRefs(e.target.value)}
          />
        </div>
        <div className="field">
          <label className="field__label" htmlFor="mcp-risk">
            Риск инструментов
          </label>
          <select
            id="mcp-risk"
            className="field__control"
            value={risk}
            onChange={(e) => setRisk(e.target.value)}
          >
            {RISKS.map((level) => (
              <option key={level} value={level}>
                {level}
              </option>
            ))}
          </select>
        </div>
        <div className="mcp__actions">
          <button
            type="button"
            className="btn"
            disabled={!command.trim() || testing}
            onClick={() => void runTest()}
          >
            {testing ? "Проверяем…" : "Проверить подключение"}
          </button>
          <button
            type="button"
            className="btn"
            disabled={!name.trim() || !command.trim()}
            onClick={() => void save()}
          >
            Сохранить
          </button>
        </div>
        {test !== null &&
          (test.ok ? (
            <p className="field__help mcp__ok" role="status">
              Подключение работает. Инструменты ({test.tools.length}):{" "}
              {test.tools.join(", ") || "—"}
            </p>
          ) : (
            <p className="field__error" role="status">
              Не подключился: {test.error}
            </p>
          ))}
        {status !== null && <p className="field__help">{status}</p>}
      </div>
    </div>
  );
}
