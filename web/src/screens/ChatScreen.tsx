import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, type Api } from "../api/client";
import { subscribeRun } from "../api/stream";
import type {
  Attachment,
  Autonomy,
  ExecutorOption,
  FileSuggestion,
  ModelCard,
  ProviderCard,
  SlashCommand,
} from "../api/types";
import { Composer } from "../components/Composer";
import { Gate } from "../components/Gate";
import { ToolCalls } from "../components/ToolCalls";
import { parseCommand, type ParsedCommand } from "../model/completion";
import { applyEvent, fromHistory, type ThreadItem } from "../model/thread";
import "./ChatScreen.css";

type Call = Extract<ThreadItem, { kind: "call" }>;
type Entry = ThreadItem | { kind: "calls"; id: string; calls: Call[] };

/** ".attachments/ab_скрин.png" → "ab_скрин.png": сегмент {name} для
    GET /sessions/{id}/attachments/{name}, а не человеку видимое имя. */
function basename(path: string): string {
  const at = path.lastIndexOf("/");
  return at < 0 ? path : path.slice(at + 1);
}

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
  onNew = () => {},
  onSessions = () => {},
}: {
  api: Api;
  sessionId: string | null;
  /** Создаёт сессию, если её ещё нет, и возвращает её id. */
  ensureSession: () => Promise<string>;
  loading?: boolean;
  error?: string | null;
  baseUrl?: string;
  token?: string;
  /** `/new` — команда чата, а не отправка агенту: заводит чат так же, как
      кнопка «Новый чат» в навигаторе (App владеет её созданием). */
  onNew?: () => void;
  /** `/sessions` — переводит фокус на навигатор, а не уходит агенту. */
  onSessions?: () => void;
}) {
  const [items, setItems] = useState<ThreadItem[]>([]);
  const [threadError, setThreadError] = useState<string | null>(null);
  const [autonomy, setAutonomy] = useState<Autonomy>("supervised");
  // Список исполнителей — с GET /executors; value конкретного варианта
  // ("native", "codex", …), а не ExecutorKind: одному kind соответствует
  // не один адаптер, кастовать value к ExecutorKind было бы молчаливо неверно.
  const [executorOptions, setExecutorOptions] = useState<ExecutorOption[]>([]);
  const [executorValue, setExecutorValue] = useState<string | null>(null);
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [providers, setProviders] = useState<ProviderCard[]>([]);
  const [models, setModels] = useState<ModelCard[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [commands, setCommands] = useState<SlashCommand[]>([]);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [, setRunId] = useState<string | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const unsubscribe = useRef<(() => void) | null>(null);
  const sendSeq = useRef(0);
  const commandSeq = useRef(0);

  useEffect(() => {
    api
      .executors()
      .then((list) => {
        setExecutorOptions(list);
        const active = list.find((option) => option.is_active);
        // Нет активного варианта — не гадаем: селект останется без выбора,
        // override уйдёт без executor (сервер возьмёт свой конфиг).
        if (active !== undefined) setExecutorValue(active.value);
      })
      .catch(() => {
        // Список не пришёл — тот же принцип: остаёмся пустыми, не гадаем.
      });
  }, [api]);

  useEffect(() => {
    api
      .commands()
      .then(setCommands)
      .catch(() => setCommands([]));
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
    // Непрочитанное вложение из прошлого чата принадлежит его workspace:
    // отправка в новом чате получила бы 400 от verify_attachment. Проще и
    // честнее сбросить, чем молча тащить путь чужой сессии дальше.
    setAttachments([]);
    setUploadError(null);
    api
      .sessionThread(sessionId)
      .then((thread) => setItems(fromHistory(thread.items)))
      .catch(() => setThreadError("Не удалось загрузить историю этой сессии."));
  }, [api, sessionId]);

  useEffect(() => () => unsubscribe.current?.(), []);

  const pushStatus = useCallback((text: string, failed: boolean) => {
    const id = `cmd-${commandSeq.current++}`;
    setItems((current) => [...current, { kind: "status", id, text, failed }]);
  }, []);

  // Действия шести команд, которые чат исполняет сам, а не отправляет агенту
  // (см. gateway/commands.py: WEB_COMMANDS). Ссылается на `items` напрямую —
  // копия для /copy должна быть самой свежей репликой на момент команды.
  const runCommand = useCallback(
    (parsed: ParsedCommand) => {
      if (parsed.name === "") {
        pushStatus(`Неизвестная команда: ${parsed.args}`, true);
        return;
      }
      if (parsed.name === "new") {
        onNew();
        return;
      }
      if (parsed.name === "sessions") {
        onSessions();
        return;
      }
      if (parsed.name === "executor" || parsed.name === "policies") {
        // Сами селекты рисует Composer — здесь только фокус по aria-label,
        // без прокидывания рефов через ещё один слой пропсов.
        const label = parsed.name === "executor" ? "Исполнитель" : "Автономия";
        document
          .querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)
          ?.focus();
        return;
      }
      if (parsed.name === "help") {
        // Список уже загружен эффектом выше (он же кормит автодополнение) —
        // второй поход на сервер здесь не нужен.
        const lines = commands.map((c) => `${c.usage} — ${c.help}`).join("\n");
        pushStatus(lines || "Команд пока нет.", false);
        return;
      }
      if (parsed.name === "copy") {
        // Буфер обмена недоступен в jsdom и в небезопасном контексте (не
        // https и не localhost) — отдельный случай, а не сбой самой команды.
        const lastSay = [...items]
          .reverse()
          .find((item) => item.kind === "say");
        if (lastSay === undefined) {
          pushStatus("Нечего копировать — Сварог ещё не ответил.", true);
          return;
        }
        if (typeof navigator.clipboard?.writeText !== "function") {
          pushStatus("Буфер обмена недоступен в этом браузере.", true);
          return;
        }
        navigator.clipboard
          .writeText(lastSay.text)
          .then(() => pushStatus("Скопировано в буфер.", false))
          .catch(() => pushStatus("Не удалось скопировать в буфер.", true));
      }
    },
    [commands, items, onNew, onSessions, pushStatus],
  );

  const send = useCallback(
    async (text: string, attachmentPaths: string[]) => {
      const parsed = parseCommand(text);
      if (parsed !== null) {
        runCommand(parsed);
        return;
      }
      // Уникальный id, а не длина списка: после удаления гейта длина
      // уменьшается, и следующая реплика получала бы занятый ключ.
      const optimisticId = `u-${sendSeq.current++}`;
      setItems((current) => [
        ...current,
        { kind: "user", id: optimisticId, text, attachments: attachmentPaths },
      ]);
      setSendError(null);
      try {
        // На чистой установке сессий нет. Молча ничего не делать — худший
        // вариант: первое действие нового пользователя уходит в тишину.
        const target = sessionId ?? (await ensureSession());
        const executorKind = executorOptions.find(
          (option) => option.value === executorValue,
        )?.kind;
        const ref = await api.sendMessage(
          target,
          text,
          autonomy,
          {
            // executor опускаем, если ещё не известен из /executors: пустое
            // поле сервер трактует как «взять из конфига», а угаданное
            // значение — как настоящий override, который переживёт конфиг.
            ...(executorKind === undefined ? {} : { executor: executorKind }),
            provider,
            model,
          },
          attachmentPaths,
        );
        setAttachments([]);
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
    [
      api,
      sessionId,
      ensureSession,
      autonomy,
      executorOptions,
      executorValue,
      provider,
      model,
      watch,
      runCommand,
    ],
  );

  const attach = useCallback(
    async (file: File) => {
      setUploadError(null);
      try {
        const target = sessionId ?? (await ensureSession());
        const stored = await api.uploadAttachment(target, file);
        setAttachments((current) => [...current, stored]);
      } catch (exc: unknown) {
        setUploadError(
          exc instanceof ApiError ? exc.message : "Не удалось загрузить файл.",
        );
      }
    },
    [api, sessionId, ensureSession],
  );

  const removeAttachment = useCallback((path: string) => {
    setAttachments((current) => current.filter((item) => item.path !== path));
  }, []);

  const onFileQuery = useCallback(
    (query: string): Promise<FileSuggestion[]> =>
      sessionId === null
        ? Promise.resolve([])
        : api.sessionFiles(sessionId, query),
    [api, sessionId],
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

  // is_active пересчитывается от выбора человека (executorValue), а не от
  // ответа сервера буквально: сервер называет активным вариант из конфига,
  // но после выбора в композере активен уже он, до следующей перезагрузки.
  const executors: ExecutorOption[] = executorOptions.map((option) => ({
    ...option,
    is_active: option.value === executorValue,
  }));

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
                  <div>{entry.text}</div>
                  {/* Строка "Вложения (...)" остаётся в тексте выше — здесь
                      только миниатюра вдобавок, не вместо неё: человек видит
                      ровно то, что получил агент, плюс картинку к нему. */}
                  {sessionId !== null && entry.attachments.length > 0 && (
                    <div className="chat__thumbs">
                      {entry.attachments.map((path) => {
                        const name = basename(path);
                        return (
                          <img
                            key={path}
                            className="chat__thumb"
                            src={`${baseUrl}/sessions/${sessionId}/attachments/${encodeURIComponent(name)}`}
                            alt={name}
                          />
                        );
                      })}
                    </div>
                  )}
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
      {uploadError !== null && (
        <p className="chat__error chat__upload-error">{uploadError}</p>
      )}
      <Composer
        onSend={(text, atts) => void send(text, atts)}
        autonomy={autonomy}
        onAutonomyChange={setAutonomy}
        executors={executors}
        onExecutorChange={setExecutorValue}
        providers={providers}
        provider={provider}
        onProviderChange={pickProvider}
        model={model}
        models={models}
        modelsError={modelsError}
        onModelChange={setModel}
        commands={commands}
        onFileQuery={onFileQuery}
        attachments={attachments}
        onAttach={(file) => void attach(file)}
        onRemoveAttachment={removeAttachment}
      />
    </div>
  );
}
