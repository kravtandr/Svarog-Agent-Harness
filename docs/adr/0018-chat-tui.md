# ADR-0018: Полноэкранный chat-TUI на Textual

## Статус

Принято (Фазы 1–4 реализованы)

| Фаза | Содержание | Статус |
| --- | --- | --- |
| 1 | `ChatEngine` — общий драйвер chat-сессии для обоих фронтендов | ✅ Сделано |
| 2 | TUI core: транскрипт с markdown-стримом, ввод с историей, статус-бар | ✅ Сделано |
| 3 | Approval/ask_user-модалки: живой гейт external и resume-цикл native | ✅ Сделано |
| 4 | Слэш-команды с автодополнением, session picker, панель событий | ✅ Сделано |

## Контекст

`svarog chat` был построчным REPL: сырой поток `on_text_delta` без
markdown, без истории ввода и навигации стрелками, с костылём
`_read_user_line` (UTF-8 по чанкам рвал кириллицу), а approval на
native-пути был виден только постфактум («ожидает approval» + подсказка
про второй терминал). Референсный UX — opencode и Claude Code:
полноэкранный чат с рендером markdown, стримингом, permission-промптами
на месте и командной строкой.

Прямое переиспользование кода референсов невозможно: fast-code —
деобфусцированный форк проприетарного Claude Code («research use only»),
opencode — TypeScript/Ink. Из них берётся только UX-модель.

Фундамент для своего TUI в ядре уже был:

* весь вывод рантайма идёт через `RunHooks` (`runtime/orchestrator.py`),
  прямых print в `runtime/llm/tools` нет — полноэкранному приложению
  ничего не мешает;
* токен-стриминг (`on_text_delta`) и прогресс (`on_progress`) уже
  события, а не печать;
* живой approval-гейт external-пути (§7) решается записью вердикта в БД
  под poll `bridge_control`; native-путь возвращает `WAITING_APPROVAL` и
  резюмируется;
* `TaskRunner.prepare_session_resources`/`SessionResources` (ADR-0017)
  дают lifecycle тёплого sandbox'а серии runs.

## Принятые развилки

1. **Textual, core-зависимость.** Asyncio-нативный фреймворк от авторов
   Rich (уже в стеке), `py.typed` (mypy strict), `MarkdownStream` —
   готовый троттленный рендер токен-стрима (нижняя граница `textual>=3.2`
   именно из-за него). Chat — основной интерфейс разработчика (§10.1),
   поэтому не extra: TUI работает из коробки. Отклонено: prompt_toolkit
   (нет layout-фреймворка полноэкранного приложения), extra `[tui]`
   (ломает «работает из коробки» ради небольшой экономии базовой
   установки).
2. **TTY-автовыбор с fallback.** `svarog chat` открывает TUI, когда stdin
   и stdout — TTY; `--plain` принудительно возвращает построчный REPL;
   без терминала (pipe, CI, CliRunner в тестах) — plain автоматически.
   Плоский режим остаётся полноценным, не деградирует.
3. **`ChatEngine` (`cli/chat_engine.py`) — общий драйвер, `RunHooks` —
   контракт фронтенда.** Тело `_chat_session` перенесено в движок без
   изменения поведения: те же native/external-ветки send, drain
   памяти/proposals после каждого сообщения, continue/fork истории,
   лимит `CHAT_HISTORY_LIMIT`. Lifecycle — `prepare_session_resources`.
   Фронтенды различаются только реализацией hooks: plain печатает
   (`_console_hooks`), TUI постит Textual-сообщения. Отклонено:
   async-generator событий вместо hooks — сломал бы симметрию с
   gateway/telegram, которые уже живут на `RunHooks`. Движок остаётся в
   слое `cli` (правило направления зависимостей: ядро не знает о CLI);
   унификация с gateway-chat — вне скоупа.
4. **Threading-модель.** Run исполняется async-worker'ом на loop'е
   Textual (без `asyncio.run` на пути TUI — loop'ом владеет приложение),
   поэтому все хуки, кроме approval, — дешёвый `post_message` с того же
   loop'а. `on_approval_requested` приходит из worker-потока bridge-гейта:
   модалка показывается через `call_from_thread`, поток блокируется на
   `threading.Event` до вердикта и сам пишет решение в БД
   (`record_gate_decision`/`record_gate_answer` — общие с plain-режимом)
   — poll гейта подхватывает, UI не блокируется. Native-путь: после
   `send` цикл `WAITING_APPROVAL → модалки → resume` (TUI-порт
   `_interactive_approvals`) — UX-улучшение относительно plain-chat,
   который только печатал подсказку.
5. **Esc = прерывание run, Ctrl+Q — выход.** Отмена worker'а роняет
   `CancelledError` в `engine.send`; write-ahead trace (ADR-0005)
   переводит run в suspended (`svarog resume` доступен), тёплый sandbox
   помечается dirty и пересобирается перед следующим сообщением
   (прецедент — `_drop_warm` gateway). Выход при активном run — двойной
   Ctrl+Q (второй прерывает и выходит).
6. **История ввода — `~/.svarog/chat_history`.** Построчный файл в
   user-state директории (конвенция `USER_CONFIG_PATH`), вне workspace —
   не попадает под `assert_workspace_isolated` и в коммиты агента.

## Компоненты

```text
cli/chat_engine.py    ChatEngine, ChatEngineProtocol (фейки в тестах),
                      record_gate_decision/answer, with_db
cli/tui/app.py        SvarogChatApp: worker-оркестрация, approval-циклы
cli/tui/hooks.py      RunHooks → Textual-сообщения (TextDelta, ToolCalled,
                      ProgressUpdated, PanelEvent) + промпт гейта
cli/tui/widgets.py    Transcript (VerticalScroll + MarkdownStream),
                      ChatInput (история ↑/↓), SlashDropdown, StatusBar,
                      EventPanel (RichLog, ^T)
cli/tui/screens.py    ApprovalScreen, QuestionScreen, SessionPickerScreen
                      (превью через fetch_sessions/session_history), Help
cli/tui/commands.py   реестр /help /new /sessions /fork /quit + автодополнение
cli/tui/history.py    InputHistory (персистентная, кап 1000)
```

## Известные trade-offs

* Тёплый sandbox серии означает для external-агента budget bridge на всю
  серию сообщений — унаследовано от CLI-chat/ADR-0017, не ново.
* `MarkdownStream.stop()` textual ≤8.x пробрасывает `CancelledError`
  своего фонового task'а (семантика py3.13+) — гасится точечно в
  `_finalize_stream`, реальная отмена worker'а не глотается.
* Панель событий копит счётчик «непрочитанного» в статус-баре, пока
  скрыта; сами события не теряются (полный след — в trace).

## Не покрыто (кандидаты на следующие фазы)

* очередь сообщений во время активного run (сейчас — отказ с подсказкой);
* поиск/фильтр в session picker и транскрипте;
* просмотр diff/артефактов из TUI;
* темизация и конфиг горячих клавиш.
