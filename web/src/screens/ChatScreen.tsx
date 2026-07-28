import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, type Api } from "../api/client";
import { subscribeRun } from "../api/stream";
import type {
  Autonomy,
  ExecutorKind,
  ExecutorOption,
  ModelCard,
  ProviderCard,
} from "../api/types";
import { Composer } from "../components/Composer";
import { Gate } from "../components/Gate";
import { ToolCalls } from "../components/ToolCalls";
import { applyEvent, fromHistory, type ThreadItem } from "../model/thread";
import "./ChatScreen.css";

type Call = Extract<ThreadItem, { kind: "call" }>;
type Entry = ThreadItem | { kind: "calls"; id: string; calls: Call[] };

/** Подряд идущие вызовы рисуются одной группой, а не по карточке на каждый. */
function groupItems(items: ThreadItem[]): Entry[] {
  const grouped: Entry[] = [];
  for (const item of items) {
    const last = grouped[grouped.length - 1];
    if (item.kind === "call") {
      if (last !== undefined && last.kind === "calls") {
        last.calls.push(item);
        continue;
      }
      grouped.push({ kind: "calls", id: `g-${item.id}`, calls: [item] });
      continue;
    }
    grouped.push(item);
  }
  return grouped;
}

export function ChatScreen({
  api,
  sessionId,
  ensureSession,
  loading = false,
  error = null,
  baseUrl = "",
  token,
}: {
  api: Api;
  sessionId: string | null;
  /** Создаёт сессию, если её ещё нет, и возвращает её id. */
  ensureSession: () => Promise<string>;
  loading?: boolean;
  error?: string | null;
  baseUrl?: string;
  token?: string;
}) {
  const [items, setItems] = useState<ThreadItem[]>([]);
  const [threadError, setThreadError] = useState<string | null>(null);
  const [autonomy, setAutonomy] = useState<Autonomy>("supervised");
  // null значит «как в конфиге, ещё не знаем»: GET /models ничего не говорит
  // про executor.type, а он живёт в GET /config — отдельным запросом.
  const [executor, setExecutor] = useState<ExecutorKind | null>(null);
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [providers, setProviders] = useState<ProviderCard[]>([]);
  const [models, setModels] = useState<ModelCard[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [, setRunId] = useState<string | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const unsubscribe = useRef<(() => void) | null>(null);
  const sendSeq = useRef(0);

  useEffect(() => {
    api
      .config()
      .then((view) => {
        const field = view.sections
          .flatMap((section) => section.fields)
          .find((item) => item.path === "executor.type");
        // Нет поля или значение не из двух известных — не гадаем: executor
        // остаётся null, override уйдёт без него (сервер возьмёт свой конфиг).
        if (
          field !== undefined &&
          (field.value === "native" || field.value === "external")
        ) {
          setExecutor(field.value);
        }
      })
      .catch(() => {
        // Конфиг не пришёл — тот же принцип: остаёмся с null, не гадаем.
      });
  }, [api]);

  useEffect(() => {
    api
      .providers()
      .then((cards) => {
        setProviders(cards);
        const active = cards.find((card) => card.is_default) ?? cards[0];
        if (active === undefined) return;
        setProvider(active.name);
        // Модель из конфига, а не литерал: подвал не должен врать про то,
        // какая модель на самом деле отвечает.
        setModel(active.model);
      })
      .catch(() => setProviders([]));
  }, [api]);

  useEffect(() => {
    if (provider === "") return;
    setModelsError(null);
    api
      .providerModels(provider)
      .then(setModels)
      .catch((exc: unknown) => {
        setModels([]);
        setModelsError(
          exc instanceof ApiError
            ? exc.message
            : "Не удалось получить список моделей у провайдера.",
        );
      });
  }, [api, provider]);

  // Смена провайдера подставляет его модель из конфига: список моделей
  // подгрузится следующим эффектом, но текущее значение не должно повиснуть
  // моделью чужого провайдера.
  const pickProvider = useCallback(
    (name: string) => {
      setProvider(name);
      setModel(providers.find((card) => card.name === name)?.model ?? "");
    },
    [providers],
  );

  const watch = useCallback(
    (runId: string) => {
      unsubscribe.current?.();
      unsubscribe.current = subscribeRun(baseUrl, runId, token, (event) =>
        setItems((current) => applyEvent(current, event)),
      );
    },
    [baseUrl, token],
  );

  useEffect(() => {
    if (sessionId === null) return;
    // Сокет прошлой сессии закрываем здесь, а не только перед отправкой:
    // иначе её события подмешиваются в ленту новой.
    unsubscribe.current?.();
    unsubscribe.current = null;
    setItems([]);
    setThreadError(null);
    setSendError(null);
    api
      .sessionThread(sessionId)
      .then((thread) => setItems(fromHistory(thread.items)))
      .catch(() => setThreadError("Не удалось загрузить историю этой сессии."));
  }, [api, sessionId]);

  useEffect(() => () => unsubscribe.current?.(), []);

  const send = useCallback(
    async (text: string) => {
      // Уникальный id, а не длина списка: после удаления гейта длина
      // уменьшается, и следующая реплика получала бы занятый ключ.
      const optimisticId = `u-${sendSeq.current++}`;
      setItems((current) => [
        ...current,
        { kind: "user", id: optimisticId, text },
      ]);
      setSendError(null);
      try {
        // На чистой установке сессий нет. Молча ничего не делать — худший
        // вариант: первое действие нового пользователя уходит в тишину.
        const target = sessionId ?? (await ensureSession());
        const ref = await api.sendMessage(target, text, autonomy, {
          // executor опускаем, если он ещё не известен из /config: пустое
          // поле сервер трактует как «взять из конфига», а угаданное
          // значение — как настоящий override, который переживёт конфиг.
          ...(executor === null ? {} : { executor }),
          provider,
          model,
        });
        setRunId(ref.run_id);
        watch(ref.run_id);
      } catch (exc: unknown) {
        // Молчаливый провал — худший исход: реплика висит в ленте, а агент
        // не запущен. Например, автономия, которую исполнитель не умеет.
        setSendError(
          exc instanceof ApiError
            ? exc.message
            : "Не удалось отправить сообщение. Проверьте, что svarog serve запущен.",
        );
        setItems((current) =>
          current.filter((item) => item.id !== optimisticId),
        );
      }
    },
    [api, sessionId, ensureSession, autonomy, executor, provider, model, watch],
  );

  const decide = useCallback(
    async (approvalId: string, approved: boolean) => {
      const ref = await api.decideApproval(approvalId, approved);
      setItems((current) =>
        current.filter(
          (item) => !(item.kind === "gate" && item.approvalId === approvalId),
        ),
      );
      // При уходе в waiting_approval сервер закрывает сокет, а resume
      // начинает новую «ногу» потока: без переподписки остаток run'а
      // не попадёт в ленту до перезагрузки истории.
      setRunId(ref.run_id);
      watch(ref.run_id);
    },
    [api, watch],
  );

  // TODO(задача 15): заменить на настоящий список из GET /executors —
  // сейчас это тот же единственный вариант из /config, обёрнутый в форму,
  // которую ждёт новый Composer, чтобы не терять уже работающее поведение.
  const executors: ExecutorOption[] =
    executor === null
      ? []
      : [
          {
            value: executor,
            kind: executor,
            adapter: null,
            available: true,
            is_active: true,
          },
        ];

  const shown = error ?? threadError ?? sendError;

  return (
    <div className="chat">
      <div className="chat__thread">
        <div className="chat__col">
          {shown !== null && <p className="chat__error">{shown}</p>}
          {shown === null && loading && (
            <p className="chat__hint">Загружаем сессии…</p>
          )}
          {/* Пустой экран — приглашение к действию, а не «нет данных». */}
          {shown === null && !loading && items.length === 0 && (
            <p className="chat__hint">
              Поставьте задачу — Сварог заведёт ветку и покажет каждый свой шаг.
            </p>
          )}
          {groupItems(items).map((entry) => {
            if (entry.kind === "calls")
              return <ToolCalls key={entry.id} calls={entry.calls} />;
            if (entry.kind === "user")
              return (
                <div key={entry.id} className="chat__you">
                  {entry.text}
                </div>
              );
            if (entry.kind === "say")
              return (
                <div key={entry.id} className="chat__say">
                  {entry.text}
                </div>
              );
            if (entry.kind === "status")
              return (
                <p
                  key={entry.id}
                  className={entry.failed ? "chat__error" : "chat__hint"}
                >
                  {entry.text}
                </p>
              );
            if (entry.kind === "gate")
              return (
                <Gate
                  key={entry.id}
                  gate={entry}
                  onDecide={(approved) =>
                    void decide(entry.approvalId, approved)
                  }
                />
              );
            return null;
          })}
        </div>
      </div>
      <Composer
        onSend={(text) => void send(text)}
        autonomy={autonomy}
        onAutonomyChange={setAutonomy}
        executors={executors}
        onExecutorChange={(value) => setExecutor(value as ExecutorKind)}
        providers={providers}
        provider={provider}
        onProviderChange={pickProvider}
        model={model}
        models={models}
        modelsError={modelsError}
        onModelChange={setModel}
        // TODO(задача 15): подключить GET /commands, @-файлы и загрузку
        // вложений — здесь заглушки, чтобы Composer компилировался и работал
        // как раньше, пока экран диалога не подключит остальной стек 10-14.
        commands={[]}
        onFileQuery={() => Promise.resolve([])}
        attachments={[]}
        onAttach={() => {}}
        onRemoveAttachment={() => {}}
      />
    </div>
  );
}
