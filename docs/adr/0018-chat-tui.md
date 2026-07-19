# ADR-0018: Inline-режим chat (диалог в обычном буфере терминала)

## Статус

Принято (реализовано)

| Фаза | Содержание | Статус |
| --- | --- | --- |
| 1 | `ChatEngine` — общий драйвер chat-сессии для всех фронтендов | ✅ Сделано |
| 2 | Inline-режим: Rich Live-стрим, readline-ввод, markdown в scrollback | ✅ Сделано |
| 3 | Approval/ask_user: живой гейт external + resume-цикл native прямо в чате | ✅ Сделано |
| 4 | Слэш-команды (/help /new /sessions /fork /copy /quit), /copy через OSC 52 | ✅ Сделано |

## Контекст

`svarog chat` был построчным REPL: сырой поток `on_text_delta` без
markdown, без истории ввода, костыль `_read_user_line` (UTF-8 по чанкам),
а approval на native-пути был виден только постфактум. Референсный UX —
qwen-code/gemini-cli: диалог живёт в **обычном буфере терминала**
(scrollback, нативное выделение и копирование), динамична только нижняя
область текущего ответа; никакого alt-screen.

Код референсов не переиспользуется: qwen-code/gemini-cli — TypeScript/Ink,
fast-code — проприетарный форк Claude Code («research use only»). Берётся
только UX-модель.

Фундамент в ядре уже был: весь вывод рантайма идёт через `RunHooks`
(прямых print в `runtime/llm/tools` нет), токен-стриминг и прогресс — уже
события, живой approval-гейт external-пути решается записью в БД под poll
`bridge_control`, native-путь возвращает `WAITING_APPROVAL` и
резюмируется, `TaskRunner.prepare_session_resources` даёт lifecycle
тёплого sandbox'а серии runs (ADR-0017).

## Принятые развилки

1. **Inline-рендер, не полноэкранный TUI.** Полноэкранный вариант на
   Textual был реализован и **отброшен по результатам живой пробы**:
   alt-screen отрезает scrollback терминала, а захват мыши ломает
   привычное выделение/копирование текста — для чата с длинными ответами
   это перевешивает выгоды экранного layout'а. Модель qwen-code:
   завершённый контент печатается в scrollback навсегда (markdown уже
   отрендерен), живёт только маленькая нижняя область (Rich `Live`:
   хвост стрима + строка прогресса, `transient=True` — стирается по
   завершении хода). Rich сам поднимает обычные `console.print` над
   Live-областью, поэтому события (tool calls, checks, commit, память)
   печатаются теми же хуками, что в plain/`run`. Новых зависимостей нет —
   Rich уже в стеке.
2. **TTY-автовыбор с fallback.** На TTY — inline-режим; `--plain` или
   отсутствие терминала (pipe, CI, CliRunner) — прежний построчный REPL.
3. **`ChatEngine` (`cli/chat_engine.py`) — общий драйвер, `RunHooks` —
   контракт фронтенда.** Тело `_chat_session` перенесено в движок без
   изменения поведения: native/external-ветки send, drain
   памяти/proposals, continue/fork, лимит `CHAT_HISTORY_LIMIT`, lifecycle
   через `prepare_session_resources`. Фронтенды различаются только
   реализацией hooks; inline переопределяет лишь `on_text_delta` (буфер
   Live) и `on_progress` (статус-строка) поверх `_console_hooks`.
   Отклонено: async-generator событий — сломал бы симметрию с
   gateway/telegram, живущими на `RunHooks`.
4. **Ввод — readline.** Стрелки/редактирование/персистентная история
   (`~/.svarog/chat_history` — user-state, вне workspace агента); с
   readline `input()` читает строку целиком, баг UTF-8 по чанкам не
   воспроизводится. Отклонено: prompt_toolkit (лишняя зависимость ради
   того же).
5. **Approval.** External-путь: живой гейт (§7) — тот же промпт, что в
   plain (`_prompt_gate_decision`, worker-поток, решение в БД под poll),
   обёрнутый паузой Live-области, чтобы промпт и перерисовка не писали в
   терминал одновременно. Native-путь: после send цикл `WAITING_APPROVAL
   → промпт → resume` прямо в чате (порт `_interactive_approvals`).
6. **Ctrl+C во время run — прервать run** (write-ahead → suspended,
   ADR-0005; sandbox помечается dirty и пересобирается перед следующим
   сообщением — прецедент `_drop_warm` gateway); Ctrl+C/Ctrl+D в промпте —
   выход. `/copy` кладёт последний ответ в буфер через OSC 52
   (iTerm2/kitty/WezTerm; в остальных — обычное выделение, оно не
   заблокировано: мышь не захватывается).

## Компоненты

```text
cli/chat_engine.py     ChatEngine, ChatEngineProtocol (фейки в тестах),
                       record_gate_decision/answer, with_db
cli/chat_inline.py     InlineChat: Rich Live-стрим, readline, слэш-команды,
                       approval-циклы, OSC 52; run_chat_inline — точка входа
cli/chat_commands.py   реестр /help /new /sessions /fork /copy /quit + parse
```

## Известные trade-offs

* Тёплый sandbox серии — budget bridge external-агента действует на всю
  серию сообщений (унаследовано от CLI-chat/ADR-0017).
* Во время стрима в Live-области виден только хвост ответа
  (`_TAIL_LINES`); полный ответ печатается по завершении хода —
  осознанный компромисс против глюков перерисовки области выше экрана.
* OSC 52 не поддерживается штатным Terminal.app — там копирование обычным
  выделением (оно работает, alt-screen'а и захвата мыши нет).

## Не покрыто (кандидаты на следующие фазы)

* очередь сообщений во время активного run;
* автодополнение слэш-команд по Tab (readline completer);
* просмотр diff/артефактов из чата.
