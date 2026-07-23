# ADR-0008: Границы MVP и порядок реализации

## Статус

Принято

## Контекст

Полный объем ТЗ — 4 интерфейса, 4 хранилища, 12 компонентов, RBAC, MCP, governance — это месяцы работы и классический путь к недостроенной платформе. Нужен минимальный вертикальный срез, который проверяет самые рискованные архитектурные решения (resumable loop, sandbox enforcement, три git-flow, single-writer память) на реальных задачах.

## Решение

### Входит в MVP

* **CLI**: `init`, `run`, `chat`, `skills list`, `skills check`, `traces list/show`, approval в терминале;
* **agent loop** как state machine с checkpoint/resume (ADR-0005);
* **tools**: read_file, write_file, edit_file, list_dir, search_files, bash, update_plan, read_skill, remember, create_skill_proposal, ask_user, request_approval (git — не tool агента, а host-flow вне sandbox, ADR-0002/0003);
* **Docker sandbox**: network off, non-root, limits, timeout (ADR-0002) + local trusted mode без sandbox;
* **skills**: loader, skill cards, on-demand загрузка, валидация metadata;
* **Policy Engine**: allow/notify/deny/require_approval, execution constraints, режимы автономии (`yolo` по умолчанию, ADR-0010), конфигурация из `policies/`;
* **Git**: flows A и C (ADR-0003); flow B — вручную через обычный git-review;
* **память**: memory repo + single writer (ADR-0004), чтение памяти в контекст;
* **SQLite**: runs, sessions, messages, tool_calls, approvals, checkpoints, memory_queue, trace;
* **LLM**: один провайдер — OpenAI-compatible API;
* **refuel** по порогу итераций — как cross-process suspend/resume (ADR-0005), поднятие `svarog resume`;
* **verifier**: только детерминированные checks (тесты, линтеры, secret scan).

### Не входит (порядок следующих итераций)

1. REST/WebSocket API + Telegram (первый внешний интерфейс);
2. MCP-интеграция;
3. Skill governance automation + **Skill Curator** (ADR-0009);
4. LLM-as-judge verifier, compaction истории;
5. Web UI;
6. Redis/Postgres/Qdrant backends, RBAC, corporate-режим.

### Критерий готовности MVP

Подмножество критериев §26 ТЗ оформлено как evals в `evals/` и проходит: init agent-home → run задачи с файлами → bash в sandbox → approval на push → trace полон → refuel на длинной задаче → resume после kill процесса.

## Последствия

* Milestones и порядок задач — `docs/first-issues.md`.
* Все интерфейсы кода (ModelProvider, QueueBackend, PolicyEngine) закладываются в MVP, даже если реализация одна — это дешево сейчас и дорого потом.

## Отклонения по состоянию на 2026-07-23

Требование «интерфейсы закладываются сразу» выполнено для `ModelProvider`
(`llm/provider.py`), `SecretStore` (`secrets/store.py`) и sandbox-бэкенда.
Последний носит имя `ExecutionEnvironment` (`sandbox/base.py`), а не
`SandboxBackend` — при ссылках на этот ADR имя стоит читать как синоним.

Два интерфейса сознательно не заведены:

* **`QueueBackend`** — сама очередь существует и работает: таблица
  `memory_queue` (ORM-класс `MemoryChange`), писатель — `enqueue_memory_change`
  в `trace/recorder.py`, единственный потребитель — `MemoryWriter.drain()`
  (`memory/writer.py`). Абстракции над ней нет намеренно: инвариант
  single-writer (ADR-0004) держится на транзакционной семантике SQLite и
  межпроцессном `LockBackend`, и подменяемый бэкенд обязан был бы
  воспроизвести оба. Интерфейс, не гарантирующий этого, вводил бы в
  заблуждение; вводить его надо вместе со второй реализацией, которая
  докажет, что инвариант переносим.
* **`PolicyEngine`** — остаётся конкретным классом (`policy/engine.py`).
  Enforcement по ADR-0002 един для всех развёртываний; подменяемая политика
  сделала бы границу безопасности расширяемой, что противоречит ADR-0002.

Пересмотреть при появлении второй реализации любого из двух.
