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
  RunOverride,
  SandboxKind,
  SandboxOption,
  SlashCommand,
} from "../api/types";
import { Composer } from "../components/Composer";
import { Gate } from "../components/Gate";
import { Markdown } from "../components/Markdown";
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

/** "ab12cd34_скрин.png" → "скрин.png": `store_attachment` (attachments.py)
    кладёт файл как "{8 hex}_{имя, как его видел человек}". Для alt/подписи
    нужно вот это имя, а не basename с хеш-префиксом — если префикс не
    похож на хеш (внешние данные, не то, что ожидали), берём basename как
    есть, а не гадаем дальше. */
function humanName(path: string): string {
  const base = basename(path);
  const match = /^[0-9a-f]{8}_(.+)$/.exec(base);
  return match !== null ? match[1] : base;
}

/** Тот же список расширений, что и `_IMAGE_MIME` в
    `tools/document_tools.py` — раздача (api.py: read_attachment) отдаёт
    именно эти суффиксы как картинку, остальное (включая .pdf/.docx/.html
    из белого списка загрузки) — как скачивание, не для <img>. */
const IMAGE_EXTENSIONS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp"]);

function isImagePath(path: string): boolean {
  const dot = path.lastIndexOf(".");
  return dot >= 0 && IMAGE_EXTENSIONS.has(path.slice(dot).toLowerCase());
}

/**
 * Миниатюра тянет байты сама через `fetch` с `Authorization`-заголовком и
 * превращает ответ в blob-URL: голый `<img src>` не может послать токен, а
 * `GET /sessions/{id}/attachments/{name}` требует его на любом не-loopback
 * bind (`api.py: _require_service`). Blob-URL, а не `?token=` в src — токен
 * в URL оседает в истории браузера и в Referer; тот приём годился только
 * для WebSocket, который не может выставить заголовок, а `fetch` может.
 */
function AttachmentThumb({
  src,
  alt,
  token,
}: {
  src: string;
  alt: string;
  token?: string;
}) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    (async () => {
      try {
        const headers: Record<string, string> = {};
        if (token !== undefined) headers.Authorization = `Bearer ${token}`;
        const response = await fetch(src, { headers });
        if (!response.ok) return;
        const blob = await response.blob();
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setBlobUrl(objectUrl);
      } catch {
        // Тихо: миниатюра просто не появится, текст с путём уже виден выше.
      }
    })();
    return () => {
      cancelled = true;
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
    };
  }, [src, token]);

  if (blobUrl === null) return null;
  return <img className="chat__thumb" src={blobUrl} alt={alt} />;
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
  // yolo — дефолт конфига (ADR-0010, runtime.autonomy): селектор стартует с
  // него же, иначе явно отправляемое supervised перекрывало бы конфиг.
  const [autonomy, setAutonomy] = useState<Autonomy>("yolo");
  // Список исполнителей — с GET /executors; value конкретного варианта
  // ("native", "codex", …), а не ExecutorKind: одному kind соответствует
  // не один адаптер, кастовать value к ExecutorKind было бы молчаливо неверно.
  const [executorOptions, setExecutorOptions] = useState<ExecutorOption[]>([]);
  const [executorValue, setExecutorValue] = useState<string | null>(null);
  // Sandbox — зеркало исполнителя: список с GET /sandboxes, выбор — свойство
  // сообщения (override), конфиг остаётся значением по умолчанию.
  const [sandboxOptions, setSandboxOptions] = useState<SandboxOption[]>([]);
  const [sandboxValue, setSandboxValue] = useState<SandboxKind | null>(null);
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [providers, setProviders] = useState<ProviderCard[]>([]);
  const [models, setModels] = useState<ModelCard[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [commands, setCommands] = useState<SlashCommand[]>([]);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploadError, setUploadError] = useState<string | null>(null);
  // Число загрузок вложений, которые ещё не ответили (успехом или
  // ошибкой) — пока оно больше нуля, отправка заблокирована: иначе Enter,
  // нажатый раньше ответа сервера, уносит сообщение без пути, которого
  // ещё не существует (Finding 8 обзора). Счётчик, а не флаг: несколько
  // файлов, брошенных разом, должны разблокировать отправку только когда
  // ответил последний.
  const [pendingUploads, setPendingUploads] = useState(0);
  const [, setRunId] = useState<string | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  // «Сварог работает…» — от отправки до ПЕРВОГО события стрима: у внешнего
  // агента между стартом (boot контейнера) и первым tool call проходят
  // десятки секунд, и без индикатора лента выглядит зависшей (31.07.2026).
  const [thinking, setThinking] = useState(false);
  // Живой run этой сессии (от отправки/переподключения до run_finished):
  // пока он идёт, отправка заблокирована — сервер всё равно ответит 409
  // «workspace занят», честнее не давать нажать (параллельные чаты).
  const [running, setRunning] = useState(false);
  const unsubscribe = useRef<(() => void) | null>(null);
  const sendSeq = useRef(0);
  const commandSeq = useRef(0);
  // Автоскролл ленты: держим низ, пока человек сам не ушёл в историю.
  const threadRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  // Сессия, которую только что завела сама загрузка вложения на чистой
  // установке (sessionId был null) — эффект смены сессии ниже не должен
  // стереть тот самый чип, ради которого сессия и была создана. Порядок,
  // в котором завершаются ensureSession() и приходит новый sessionId-проп,
  // не гарантирован (React не обязан прогонять их в каком-то одном
  // порядке) — сравнение по id, а не расчёт на конкретную гонку.
  const justCreatedSessionId = useRef<string | null>(null);
  // Общая точка резолва сессии для attach() и send(): без неё оба метода
  // независимо видят sessionId===null в одном и том же тике (N файлов,
  // брошенных разом; Enter раньше, чем родитель перерисовался с id от
  // attach()) и зовут ensureSession() порознь, заводя не одну сессию, а
  // несколько (Finding 7 обзора). Кешируем сам промис, а не готовый id —
  // конкурентные вызовы подписываются на него, ещё не зная результата.
  const pendingSession = useRef<Promise<string> | null>(null);
  // Метка "эпохи" активной сессии — растёт при настоящем переключении чата
  // (не при первом появлении сессии, которую только что создал сам
  // attach()). Загрузка, начатая до переключения и ответившая после,
  // сверяет эпоху и не кладёт путь из workspace прошлой сессии в
  // attachments уже новой.
  const sessionEpoch = useRef(0);

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
      .sandboxes()
      .then((list) => {
        setSandboxOptions(list);
        const active = list.find((option) => option.is_active);
        if (active !== undefined) setSandboxValue(active.value);
      })
      .catch(() => {
        // Как с /executors: нет списка — селект пуст, override без sandbox.
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
      unsubscribe.current = subscribeRun(baseUrl, runId, token, (event) => {
        // Первое же событие стрима гасит индикатор «работает»: дальше
        // прогресс виден по самим событиям (чипы, дельты текста).
        setThinking(false);
        if (event.type === "run_finished") setRunning(false);
        setItems((current) => applyEvent(current, event));
      });
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
    setThinking(false);
    stickToBottom.current = true; // новая сессия открывается свежим низом
    // Эта сессия теперь известна родителю — общему резолверу больше не за
    // что держаться. Следующий раз, когда sessionId снова станет null
    // (например, после "/new"), resolveTarget() обязан позвать
    // ensureSession() заново, а не отдать кеш от этой сессии.
    pendingSession.current = null;
    if (justCreatedSessionId.current === sessionId) {
      // Эта сессия только что создана самим resolveTarget() (см. ниже) —
      // вложение, ради которого она появилась, должно пережить это переключение.
      justCreatedSessionId.current = null;
    } else {
      // Непрочитанное вложение из прошлого чата принадлежит его workspace:
      // отправка в новом чате получила бы 400 от verify_attachment. Проще и
      // честнее сбросить, чем молча тащить путь чужой сессии дальше.
      setAttachments([]);
      setUploadError(null);
      // Настоящее переключение чата (а не первое появление только что
      // созданной сессии) — гасим загрузки, начатые для прошлой сессии:
      // их результат не должен долететь до attachments уже новой.
      sessionEpoch.current += 1;
    }
    setRunning(false);
    api
      .sessionThread(sessionId)
      .then((thread) => {
        const history = fromHistory(thread.items);
        if (thread.live_run_id) {
          // В сессии прямо сейчас идёт run (параллельные чаты): рисуем
          // пузырь его задачи и переподписываемся — WS-реплей истории
          // событий восстановит вызовы и текст, дальше лента живая.
          setItems([
            ...history,
            {
              kind: "user",
              id: `live-${thread.live_run_id}`,
              text: thread.live_task ?? "",
              attachments: [],
            },
          ]);
          setThinking(true);
          setRunning(true);
          watch(thread.live_run_id);
        } else {
          setItems(history);
        }
      })
      .catch(() => setThreadError("Не удалось загрузить историю этой сессии."));
  }, [api, sessionId, watch]);

  useEffect(() => () => unsubscribe.current?.(), []);

  // Свежие события подтягивают ленту вниз — но только пока человек и так у
  // низа: ушедшего читать историю автоскролл не дёргает.
  useEffect(() => {
    const el = threadRef.current;
    if (el !== null && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [items]);

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

  // Единственное место, где sessionId===null превращается в настоящий id.
  // send() и attach() зовут это, а не ensureSession() напрямую — иначе на
  // чистой установке они (или несколько attach() подряд, см. attach())
  // видят один и тот же null в один и тот же тик и заводят по сессии на
  // каждого вместо одной общей (Finding 7 обзора).
  const resolveTarget = useCallback(async (): Promise<string> => {
    if (sessionId !== null) return sessionId;
    if (pendingSession.current === null) {
      const promise = ensureSession().then((id) => {
        // Помечаем сразу после получения id, а не после того, как отработает
        // вызвавший resolveTarget() код: эффект смены сессии может сработать
        // в любой момент между этими двумя строками, и метка обязана стоять
        // раньше.
        justCreatedSessionId.current = id;
        return id;
      });
      // Отклонённый promise чистим сами: эффект на sessionId!==null (см.
      // выше) здесь не сработает — сессия так и не появилась. Без этого
      // каждая следующая отправка ждала бы тот же отклонённый promise и
      // падала бы с той же ошибкой, даже когда сервер уже отвечает.
      pendingSession.current = promise.catch((error) => {
        pendingSession.current = null;
        throw error;
      });
    }
    return pendingSession.current;
  }, [sessionId, ensureSession]);

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
      setThinking(true);
      setRunning(true);
      try {
        // Тот же резолвер, что у attach(): сессия, которую уже завела (или
        // заводит прямо сейчас) загрузка вложения, а не вторая новая поверх
        // неё (Finding 7 обзора, гонка "Enter раньше перерисовки").
        const target = await resolveTarget();
        const selectedExecutor = executorOptions.find(
          (option) => option.value === executorValue,
        );
        const ref = await api.sendMessage(
          target,
          text,
          autonomy,
          {
            // executor/adapter опускаем, если вариант ещё не известен из
            // /executors: пустое поле сервер трактует как «взять из
            // конфига», а угаданное значение — как настоящий override,
            // который переживёт конфиг. adapter у native всё равно null —
            // client.ts опускает его наравне с undefined.
            ...(selectedExecutor === undefined
              ? {}
              : {
                  executor: selectedExecutor.kind,
                  // GET /executors отдаёт adapter простой строкой
                  // (ExecutorOptionView.adapter: str | None) — сервер не
                  // сужает её до Literal, в отличие от SendMessageRequest.
                  // Сужаем здесь: значения приходят из того же перечня
                  // адаптеров, что и Literal, которого ждёт сообщение.
                  adapter: (selectedExecutor.adapter ??
                    undefined) as RunOverride["adapter"],
                }),
            // Sandbox — тот же принцип: не пришёл список или выбора нет —
            // поле опускаем, сервер берёт конфиг.
            ...(sandboxValue === null ? {} : { sandbox: sandboxValue }),
            provider,
            model,
          },
          attachmentPaths,
        );
        setAttachments([]);
        // Баннер прошлой неудачной загрузки не должен пережить успешную
        // отправку — иначе он висит бессрочно, даже когда проблема уже не
        // актуальна.
        setUploadError(null);
        setRunId(ref.run_id);
        watch(ref.run_id);
      } catch (exc: unknown) {
        // Молчаливый провал — худший исход: реплика висит в ленте, а агент
        // не запущен. Например, автономия, которую исполнитель не умеет.
        setThinking(false);
        setRunning(false);
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
      resolveTarget,
      autonomy,
      executorOptions,
      executorValue,
      sandboxValue,
      provider,
      model,
      watch,
      runCommand,
    ],
  );

  const attach = useCallback(
    async (file: File) => {
      setUploadError(null);
      // Эпоха на момент старта — не на момент ответа сервера: пока файл
      // грузится, могут по-настоящему переключить чат (Finding 7 обзора,
      // гонка "загрузка поперёк переключения").
      const epoch = sessionEpoch.current;
      setPendingUploads((n) => n + 1);
      try {
        const target = await resolveTarget();
        const stored = await api.uploadAttachment(target, file);
        if (sessionEpoch.current !== epoch) {
          // Сессию сменили, пока файл грузился — путь принадлежит workspace
          // прошлой сессии и не должен попасть в чипы уже другой.
          return;
        }
        setAttachments((current) => [...current, stored]);
      } catch (exc: unknown) {
        if (sessionEpoch.current !== epoch) return;
        setUploadError(
          exc instanceof ApiError ? exc.message : "Не удалось загрузить файл.",
        );
      } finally {
        setPendingUploads((n) => n - 1);
      }
    },
    [api, resolveTarget],
  );

  const removeAttachment = useCallback((path: string) => {
    setAttachments((current) => current.filter((item) => item.path !== path));
    // Крестик — тоже способ закрыть баннер прошлой ошибки загрузки: без
    // этого 415 от несвязанного файла висел бы над композером бессрочно.
    setUploadError(null);
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
      // Сокет НЕ переподписываем: waiting_approval больше не закрывает стрим
      // (storage/events.py), и события resume придут в уже открытый. Прежняя
      // переподписка реплеила бэклог со старым approval_required — гейт
      // возвращался бесконечно (петля «Разрешить», 2026-07-30).
      setItems((current) =>
        current.filter(
          (item) =>
            !(item.kind === "gate" && item.approvalId === approvalId) &&
            // Плашка «Запуск ждёт вашего решения» отработала — решение есть.
            !(
              item.kind === "status" &&
              item.text.includes("ждёт вашего решения")
            ),
        ),
      );
      setRunId(ref.run_id);
    },
    [api],
  );

  // is_active пересчитывается от выбора человека (executorValue), а не от
  // ответа сервера буквально: сервер называет активным вариант из конфига,
  // но после выбора в композере активен уже он, до следующей перезагрузки.
  const executors: ExecutorOption[] = executorOptions.map((option) => ({
    ...option,
    is_active: option.value === executorValue,
  }));

  const sandboxes: SandboxOption[] = sandboxOptions.map((option) => ({
    ...option,
    is_active: option.value === sandboxValue,
  }));

  const shown = error ?? threadError ?? sendError;

  return (
    <div className="chat">
      <div
        className="chat__thread"
        ref={threadRef}
        onScroll={() => {
          const el = threadRef.current;
          if (el === null) return;
          // «Прилипание» к низу выключается, когда человек ушёл читать
          // историю, и включается обратно у самого низа.
          stickToBottom.current =
            el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
      >
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
                  {/* Строка "Вложения (...)" остаётся в entry.text как есть
                      (thread.ts её не вырезает) — здесь только миниатюра
                      вдобавок, не вместо неё: человек видит ровно то, что
                      получил агент, плюс картинку/подпись к нему. */}
                  <div>{entry.text}</div>
                  {sessionId !== null && entry.attachments.length > 0 && (
                    <div className="chat__thumbs">
                      {entry.attachments.map((path) => {
                        const name = humanName(path);
                        const src = `${baseUrl}/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(basename(path))}`;
                        if (isImagePath(path)) {
                          return (
                            <AttachmentThumb
                              key={path}
                              src={src}
                              alt={name}
                              token={token}
                            />
                          );
                        }
                        // Не картинка (.pdf/.docx/.html/...) — именованный
                        // чип, а не сломанный <img>: раздача для таких
                        // файлов идёт как скачивание (api.py: read_attachment),
                        // а не inline.
                        return (
                          <span key={path} className="chat__doc">
                            📄 {name}
                          </span>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            if (entry.kind === "say")
              return (
                <div key={entry.id} className="chat__say">
                  <Markdown text={entry.text} />
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
          {thinking && (
            <p className="chat__hint chat__thinking" role="status">
              Сварог работает…
            </p>
          )}
        </div>
      </div>
      {uploadError !== null && (
        <p className="chat__error chat__upload-error">{uploadError}</p>
      )}
      {/* Единственная видимая примета того, что файл вообще грузится —
          без неё вставка большого файла выглядит так, будто ничего не
          произошло (Finding 8 обзора). Отправка при этом заблокирована
          через prop `uploading` у Composer — баннер сам по себе гонку не
          снимает, только показывает, что она ещё не кончилась. */}
      {uploadError === null && pendingUploads > 0 && (
        <p className="chat__upload-pending" role="status">
          Загружаем файл…
        </p>
      )}
      <Composer
        onSend={(text, atts) => void send(text, atts)}
        uploading={pendingUploads > 0}
        busy={running}
        autonomy={autonomy}
        onAutonomyChange={setAutonomy}
        executors={executors}
        onExecutorChange={setExecutorValue}
        sandboxes={sandboxes}
        onSandboxChange={setSandboxValue}
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
