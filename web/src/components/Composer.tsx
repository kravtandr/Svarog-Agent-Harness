import { useEffect, useRef, useState } from "react";

import type {
  Attachment,
  Autonomy,
  ExecutorOption,
  FileSuggestion,
  ModelCard,
  ProviderCard,
  SandboxKind,
  SandboxOption,
  SlashCommand,
} from "../api/types";
import {
  detectCompletion,
  replaceToken,
  type CompletionQuery,
} from "../model/completion";
import { Attachments } from "./Attachments";
import {
  Completion,
  COMPLETION_LISTBOX_ID,
  type CompletionItem,
} from "./Completion";
import { ModelPicker } from "./ModelPicker";
import "./Composer.css";

/** Автономия — конечный список из схемы конфига (config/schema.py); тот же
    список рисуют настройки через field.choices. Отдельного справочника
    русских подписей больше нет: значение показывается как есть. */
const AUTONOMY_MODES: Autonomy[] = ["supervised", "auto", "yolo"];

const UNAVAILABLE_HINT =
  "Ни CLI агента в PATH, ни собранного docker-образа не найдено";
const SANDBOX_UNAVAILABLE_HINT = "docker/podman не найден на хосте";

/* Иконки — рукописные SVG по сетке 18px (выбор «вариант C», 04.08.2026):
   эмодзи в кнопках зависели от платформенного шрифта и выглядели чужими. */
function ClipIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path
        d="M14.5 6.5l-6 6a3.2 3.2 0 01-4.5-4.5l6.5-6.5a2.1 2.1 0 013 3l-6.5 6.5a1 1 0 01-1.5-1.5l6-6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function MicIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <rect
        x="6.5"
        y="2"
        width="5"
        height="9"
        rx="2.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
      />
      <path
        d="M3.5 9a5.5 5.5 0 0011 0M9 14.5V16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

function UpIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 18 18" aria-hidden="true">
      <path
        d="M9 14V4M4.5 8.5L9 4l4.5 4.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function Composer({
  onSend,
  uploading = false,
  busy = false,
  autonomy,
  onAutonomyChange,
  executors,
  onExecutorChange,
  sandboxes,
  onSandboxChange,
  providers,
  provider,
  onProviderChange,
  model,
  models,
  modelsError,
  onModelChange,
  commands,
  onFileQuery,
  attachments,
  onAttach,
  onRemoveAttachment,
}: {
  onSend: (text: string, attachments: string[]) => void;
  /** Пока хоть одна загрузка вложения не ответила — сервер ещё не отдал
      путь для неё, и отправка сообщения без него ушла бы молча без файла
      (Finding 8 обзора). По умолчанию false — экраны без вложений (если
      такие появятся) не обязаны про это думать. */
  uploading?: boolean;
  /** В сессии прямо сейчас идёт run: отправка заблокирована — сервер всё
      равно ответит 409 «workspace занят» (параллельные чаты: пишите в
      другой чат, этот доделает и освободится). */
  busy?: boolean;
  autonomy: Autonomy;
  onAutonomyChange: (autonomy: Autonomy) => void;
  /** GET /executors: нативный цикл плюс по одной записи на адаптер;
      is_active помечает текущий выбор, available — установлен ли его CLI. */
  executors: ExecutorOption[];
  onExecutorChange: (value: string) => void;
  /** GET /sandboxes: docker/local-trusted; available — есть ли docker-runtime. */
  sandboxes: SandboxOption[];
  onSandboxChange: (value: SandboxKind) => void;
  providers: ProviderCard[];
  provider: string;
  onProviderChange: (name: string) => void;
  model: string;
  models: ModelCard[];
  modelsError: string | null;
  onModelChange: (id: string) => void;
  /** GET /commands — список для «/»-автодополнения, фильтруется на клиенте. */
  commands: SlashCommand[];
  /** GET /sessions/{id}/files?q= — «@»-автодополнение просит сервер на
      каждый токен, локального списка файлов у композера нет. */
  onFileQuery: (query: string) => Promise<FileSuggestion[]>;
  attachments: Attachment[];
  onAttach: (file: File) => void;
  onRemoveAttachment: (path: string) => void;
}) {
  const [text, setText] = useState("");
  const [picking, setPicking] = useState(false);
  const [query, setQuery] = useState<CompletionQuery>({
    mode: "idle",
    token: "",
  });
  const [active, setActive] = useState(0);
  // Закрыто Escape'ом — до следующего изменения поля или сдвига курсора:
  // они же сбрасывают этот флаг, так что меню не может застрять скрытым.
  const [dismissed, setDismissed] = useState(false);
  const [files, setFiles] = useState<FileSuggestion[]>([]);
  const field = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  // Курсор, который нужно выставить после следующего рендера (setSelectionRange
  // на контролируемом textarea имеет смысл только после того, как React
  // применит новое value — раньше вызов просто не найдёт нужной позиции).
  const pendingCaret = useRef<number | null>(null);
  const fileRequest = useRef(0);

  const activeExecutor = executors.find((option) => option.is_active);
  // claude-code — единственный исполнитель со «своим» провайдером (подписка,
  // anthropic-endpoint): выбор провайдера/модели из нашего каталога для него
  // не имеет смысла. native/opencode/codex ходят к OpenAI-совместимому
  // провайдеру из карточки — им и провайдер, и модель переключаются отсюда
  // (модель доезжает: opencode — managed-конфиг, codex — `-m`).
  const providerLocked = activeExecutor?.adapter === "claude-code";

  // «@»-подсказки — сетевой запрос на каждый токен: список файлов рабочей
  // копии не тащим на клиент целиком (см. комментарий к решению в §3
  // 2026-07-28-composer-completion-and-uploads-design.md — при лимите
  // at_suggestions в 12 файлов кешировать и фильтровать на клиенте смысла
  // нет). Дебаунс — иначе быстрый набор "@скрин" шлёт запрос на каждую
  // нажатую букву. Более поздний ответ на устаревший запрос
  // игнорируется через тикет — иначе медленный ответ на "@a" мог бы
  // перезаписать уже показанные подсказки для "@ab".
  useEffect(() => {
    if (query.mode !== "at") {
      setFiles([]);
      return;
    }
    const ticket = ++fileRequest.current;
    const timer = window.setTimeout(() => {
      onFileQuery(query.token.slice(1))
        .then((result) => {
          if (fileRequest.current === ticket) setFiles(result);
        })
        .catch(() => {
          if (fileRequest.current === ticket) setFiles([]);
        });
    }, 180);
    return () => window.clearTimeout(timer);
  }, [query.mode, query.token, onFileQuery]);

  useEffect(() => {
    if (pendingCaret.current === null) return;
    field.current?.setSelectionRange(
      pendingCaret.current,
      pendingCaret.current,
    );
    pendingCaret.current = null;
  }, [text]);

  const items: CompletionItem[] =
    dismissed || query.mode === "idle"
      ? []
      : query.mode === "slash"
        ? commands
            // chat_completion.py:86 (сервер CLI) сравнивает без учёта
            // регистра — "/HE" там находит "/help". Без .toLowerCase() тут
            // тот же ввод в вебе не находил ничего.
            .filter((c) =>
              `/${c.name}`.toLowerCase().startsWith(query.token.toLowerCase()),
            )
            .map((c) => ({
              value: `/${c.name}`,
              label: `/${c.name}`,
              description: c.help,
            }))
        : files.map((f) => ({
            value: `@${f.path}`,
            label: f.path,
            description: "файл",
          }));

  // Общая точка для «поле изменилось» и «курсор сдвинулся без изменения
  // текста» (стрелки, клик в середину строки) — оба случая должны заново
  // определить режим подсказок и открыть меню, если оно было закрыто Escape.
  function detect(value: string, caret: number) {
    setQuery(detectCompletion(value.slice(0, caret)));
    setDismissed(false);
    setActive(0);
  }

  function pick(value: string) {
    const caret = field.current?.selectionStart ?? text.length;
    const result = replaceToken(text, caret, value);
    setText(result.text);
    // replaceToken дописывает пробел — detectCompletion на новом тексте сам
    // вернёт idle, и меню закроется без отдельного вызова setDismissed.
    setQuery(detectCompletion(result.text.slice(0, result.caret)));
    setActive(0);
    pendingCaret.current = result.caret;
  }

  function attach(list: FileList | null | undefined) {
    if (list === null || list === undefined) return;
    // Не Array.from(list).forEach(onAttach) — forEach зовёт колбэк с
    // (элемент, индекс, массив), и onAttach получил бы лишние аргументы.
    Array.from(list).forEach((file) => onAttach(file));
  }

  function send() {
    // Хоть одна загрузка ещё не ответила — путь для неё ещё не существует.
    // Отправить сейчас значит молча уйти без этого файла (Finding 8
    // обзора); правильнее подождать ответа, будь он успехом или ошибкой.
    if (uploading || busy) return;
    const trimmed = text.trim();
    if (!trimmed) return;
    onSend(
      trimmed,
      attachments.map((item) => item.path),
    );
    setText("");
    setQuery({ mode: "idle", token: "" });
  }

  return (
    <div className="composer">
      <div className="composer__inner">
        {picking && (
          <div className="composer__picker">
            <ModelPicker
              models={models}
              current={model}
              error={modelsError}
              providers={providers}
              provider={provider}
              onProviderChange={onProviderChange}
              onPick={(id) => {
                onModelChange(id);
                setPicking(false);
              }}
              onClose={() => setPicking(false)}
            />
          </div>
        )}
        {/* Completion сама ничего не рисует для пустого списка и сама себя
            позиционирует над полем — обёртка ей не нужна. */}
        <Completion items={items} active={active} onPick={pick} />
        <div className="composer__box">
          {/* Attachments точно так же сама скрывается, когда вложений нет. */}
          <Attachments items={attachments} onRemove={onRemoveAttachment} />
          <textarea
            ref={field}
            className="composer__field"
            aria-label="Написать Сварогу"
            placeholder="Написать Сварогу"
            rows={1}
            value={text}
            // Комбобокс, а не голый textbox: без role="combobox" aria-expanded
            // не имеет смысла на textarea, а без aria-controls
            // role="listbox" у Completion — сирота, с которым программа
            // чтения с экрана эту подсказку никак не связывает.
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={items.length > 0}
            aria-controls={items.length > 0 ? COMPLETION_LISTBOX_ID : undefined}
            aria-activedescendant={
              items.length > 0
                ? `${COMPLETION_LISTBOX_ID}-option-${active}`
                : undefined
            }
            onChange={(event) => {
              const value = event.target.value;
              setText(value);
              detect(value, event.target.selectionStart ?? value.length);
            }}
            onSelect={(event) => {
              const el = event.currentTarget;
              detect(el.value, el.selectionStart ?? el.value.length);
            }}
            onPaste={(event) => {
              const pasted = event.clipboardData?.files;
              if (pasted !== undefined && pasted.length > 0) {
                // Иначе путь/имя файла заодно вставится как текст.
                event.preventDefault();
                attach(pasted);
              }
            }}
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => {
              if (event.dataTransfer.files.length > 0) {
                event.preventDefault();
                attach(event.dataTransfer.files);
              }
            }}
            onKeyDown={(event) => {
              if (items.length > 0) {
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  setActive((i) => (i + 1) % items.length);
                  return;
                }
                if (event.key === "ArrowUp") {
                  event.preventDefault();
                  setActive((i) => (i - 1 + items.length) % items.length);
                  return;
                }
                if (event.key === "Escape") {
                  event.preventDefault();
                  setDismissed(true);
                  return;
                }
                if (event.key === "Enter" || event.key === "Tab") {
                  // Пока меню открыто, Enter вставляет подсказку, а не отправляет:
                  // иначе первое же дополнение улетит агенту недописанным.
                  event.preventDefault();
                  pick(items[active].value);
                  return;
                }
              }
              // Enter отправляет, Shift+Enter — перенос строки: так ведёт
              // себя любой чат, и без этого поле кажется сломанным.
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                send();
              }
            }}
          />
          <div className="composer__foot">
            {/* Автономия — свойство сообщения, её принимает POST /sessions/{id}/messages.
                Исполнитель, провайдер и модель — тоже свойство сообщения (override):
                конфиг остаётся значением по умолчанию, а не единственным вариантом. */}
            <span className="composer__modes">
              <select
                className="composer__select"
                aria-label="Автономия"
                value={autonomy}
                onChange={(event) =>
                  onAutonomyChange(event.target.value as Autonomy)
                }
              >
                {AUTONOMY_MODES.map((mode) => (
                  <option key={mode} value={mode}>
                    {mode}
                  </option>
                ))}
              </select>
              <span className="composer__dot" aria-hidden="true">
                ·
              </span>
              <select
                className="composer__select"
                aria-label="Исполнитель"
                value={activeExecutor?.value ?? ""}
                // Список ещё не пришёл от GET /executors — угаданный выбор
                // хуже пустого, поэтому пока нечего показывать, кроме заглушки.
                disabled={executors.length === 0}
                onChange={(event) => onExecutorChange(event.target.value)}
              >
                {executors.length === 0 && (
                  <option value="" disabled>
                    исполнитель…
                  </option>
                )}
                {executors.map((option) => (
                  <option
                    key={option.value}
                    value={option.value}
                    // Недоступный вариант виден и назван, а не спрятан: иначе
                    // человек без codex в PATH решит, что Сварог его не умеет.
                    disabled={!option.available}
                    title={option.available ? undefined : UNAVAILABLE_HINT}
                  >
                    {option.value}
                  </option>
                ))}
              </select>
              <span className="composer__dot" aria-hidden="true">
                ·
              </span>
              <select
                className="composer__select"
                aria-label="Sandbox"
                value={
                  sandboxes.find((option) => option.is_active)?.value ?? ""
                }
                disabled={sandboxes.length === 0}
                onChange={(event) =>
                  onSandboxChange(event.target.value as SandboxKind)
                }
              >
                {sandboxes.length === 0 && (
                  <option value="" disabled>
                    sandbox…
                  </option>
                )}
                {sandboxes.map((option) => (
                  <option
                    key={option.value}
                    value={option.value}
                    disabled={!option.available}
                    title={
                      option.available ? undefined : SANDBOX_UNAVAILABLE_HINT
                    }
                  >
                    {option.value}
                  </option>
                ))}
              </select>
              <span className="composer__dot" aria-hidden="true">
                ·
              </span>
              {providers.length > 1 && (
                <select
                  className="composer__select"
                  aria-label="Провайдер"
                  value={provider}
                  disabled={providerLocked}
                  title={
                    providerLocked
                      ? "claude-code ходит к своему провайдеру (подписка)"
                      : undefined
                  }
                  onChange={(event) => onProviderChange(event.target.value)}
                >
                  {providers.map((card) => (
                    <option key={card.name} value={card.name}>
                      {card.name}
                    </option>
                  ))}
                </select>
              )}
              <button
                type="button"
                className="composer__model"
                aria-label="Выбрать модель"
                disabled={providerLocked}
                title={
                  providerLocked
                    ? "Модель claude-code определяется его подпиской"
                    : "Выбрать модель"
                }
                onClick={() => setPicking(true)}
              >
                {model === "" ? "модель…" : model}
              </button>
            </span>
            <span className="composer__spacer" />
            {/* Кнопки действий — одной группой: при переносе строки они
                уходят вместе и остаются прижаты вправо, а не рассыпаются
                (send падал на отдельную строку после добавления sandbox). */}
            <span className="composer__actions">
              <input
                ref={fileInput}
                type="file"
                multiple
                className="composer__hidden"
                // Управляется только кнопкой рядом: собственного имени ей не
                // нужно, а видимой для скринридера — тем более, второй контрол
                // с тем же смыслом только запутает.
                aria-hidden="true"
                tabIndex={-1}
                onChange={(event) => {
                  attach(event.target.files);
                  event.target.value = "";
                }}
              />
              <button
                type="button"
                className="composer__icon"
                aria-label="Прикрепить файл"
                onClick={() => fileInput.current?.click()}
              >
                <ClipIcon />
              </button>
              {/* Место под голос занято сразу: включение не потребует переверстки. */}
              <button
                type="button"
                className="composer__icon"
                aria-label="Голосовой ввод"
                aria-describedby="mic-hint"
                disabled
              >
                <MicIcon />
              </button>
              <span id="mic-hint" className="composer__hidden">
                Голосовой ввод появится позже
              </span>
              <button
                type="button"
                className="composer__send"
                disabled={uploading || busy}
                title={
                  busy
                    ? "В этом чате идёт run — дождитесь или напишите в другой чат"
                    : uploading
                      ? "Дождитесь загрузки файла"
                      : undefined
                }
                onClick={send}
              >
                Отправить <UpIcon />
              </button>
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
