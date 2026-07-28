import { useCallback, useEffect, useState } from "react";

import type { Api } from "../api/client";
import type { RunDetail, RunDiff, RunSummary } from "../api/types";
import "./RunsScreen.css";

const STATE_LABELS: Record<string, string> = {
  pending: "в очереди",
  running: "идёт",
  waiting_approval: "ждёт решения",
  suspended: "приостановлен",
  completed: "завершён",
  failed: "упал",
  cancelled: "отменён",
};

/** Живое, ожидание, провал и всё остальное — три состояния, как в ленте. */
function stateKind(state: string): "run" | "wait" | "error" | "done" {
  if (state === "running" || state === "pending") return "run";
  if (state === "waiting_approval" || state === "suspended") return "wait";
  if (state === "failed" || state === "cancelled") return "error";
  return "done";
}

function DiffBlock({ title, patch }: { title: string; patch: string }) {
  if (patch.trim() === "") return null;
  return (
    <>
      <h4 className="runs__subtitle">{title}</h4>
      <pre className="runs__diff">
        {patch.split("\n").map((line, index) => (
          <span
            key={index}
            className={
              line.startsWith("+") && !line.startsWith("+++")
                ? "runs__diff-add"
                : line.startsWith("-") && !line.startsWith("---")
                  ? "runs__diff-del"
                  : undefined
            }
          >
            {line}
          </span>
        ))}
      </pre>
    </>
  );
}

/**
 * Запуски: то же, что `svarog traces list` и `traces show`.
 *
 * Здесь видно, чем run кончился и что он изменил, — это и есть аудит,
 * ради которого Сварог хранит полный trace.
 */
export function RunsScreen({ api }: { api: Api }) {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [diff, setDiff] = useState<RunDiff | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .runs()
      .then(setRuns)
      .catch(() => setError("Не удалось загрузить запуски."));
  }, [api]);

  const open = useCallback(
    async (runId: string) => {
      setDiff(null);
      setDetail(await api.run(runId));
      // Дифф — отдельный запрос: он тяжелее и нужен не всегда.
      setDiff(await api.runDiff(runId));
    },
    [api],
  );

  if (error !== null) return <p className="runs__error">{error}</p>;
  if (runs === null) return <p className="runs__hint">Загружаем запуски…</p>;
  if (runs.length === 0)
    return (
      <p className="runs__hint">
        Запусков пока нет — поставьте первую задачу в диалоге.
      </p>
    );

  return (
    <div className="runs">
      <div className="runs__list">
        {runs.map((run) => (
          <button
            key={run.run_id}
            type="button"
            className={`runs__item${detail?.run_id === run.run_id ? " runs__item--active" : ""}`}
            onClick={() => void open(run.run_id)}
          >
            <span
              className={`runs__state runs__state--${stateKind(run.state)}`}
            >
              {STATE_LABELS[run.state] ?? run.state}
            </span>
            <span className="runs__task">{run.task}</span>
            <span className="runs__meta">
              {run.iterations} шагов · {run.tokens_used} токенов
            </span>
          </button>
        ))}
      </div>

      <div className="runs__detail">
        {detail === null ? (
          <p className="runs__hint">
            Выберите запуск, чтобы увидеть его трейс.
          </p>
        ) : (
          <>
            <div className="runs__id">{detail.run_id}</div>
            <h3 className="runs__title">{detail.task}</h3>
            <div className="runs__facts">
              <span>{STATE_LABELS[detail.state] ?? detail.state}</span>
              <span>автономия: {detail.autonomy}</span>
              <span>{detail.iterations} шагов</span>
              <span>{detail.tokens_used} токенов</span>
              <span>${detail.cost_usd.toFixed(2)}</span>
            </div>
            {detail.error !== null && (
              <p className="runs__failure">{detail.error}</p>
            )}

            <h4 className="runs__subtitle">
              Вызовы ({detail.tool_calls.length})
            </h4>
            {detail.tool_calls.map((call, index) => (
              <div key={index} className="runs__call">
                <span className="runs__call-name">{call.tool_name}</span>
                <span className="runs__call-status">{call.status}</span>
                {call.policy_decision !== null && (
                  <span className="runs__call-policy">
                    {call.policy_decision}
                  </span>
                )}
                {call.error !== null && (
                  <span className="runs__call-error">{call.error}</span>
                )}
              </div>
            ))}

            {diff !== null && (
              <>
                <DiffBlock title="Закоммичено" patch={diff.committed} />
                <DiffBlock title="Не закоммичено" patch={diff.uncommitted} />
                {diff.committed.trim() === "" &&
                  diff.uncommitted.trim() === "" && (
                    <p className="runs__hint">
                      Этот запуск ничего не изменил в рабочем дереве.
                    </p>
                  )}
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
