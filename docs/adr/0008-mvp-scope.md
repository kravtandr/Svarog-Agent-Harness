# ADR-0008: Границы MVP и порядок реализации

## Статус

Принято

## Контекст

Полный объем ТЗ — 4 интерфейса, 4 хранилища, 12 компонентов, RBAC, MCP, governance — это месяцы работы и классический путь к недостроенной платформе. Нужен минимальный вертикальный срез, который проверяет самые рискованные архитектурные решения (resumable loop, sandbox enforcement, три git-flow, single-writer память) на реальных задачах.

## Решение

### Входит в MVP

* **CLI**: `init`, `run`, `chat`, `skills list`, `skills check`, `traces list/show`, approval в терминале;
* **agent loop** как state machine с checkpoint/resume (ADR-0005);
* **tools**: read_file, write_file, edit_file, list_dir, search_files, bash, git, read_skill, ask_user, request_approval;
* **Docker sandbox**: network off, non-root, limits, timeout (ADR-0002) + local trusted mode без sandbox;
* **skills**: loader, skill cards, on-demand загрузка, валидация metadata;
* **Policy Engine**: allow/notify/deny/require_approval, execution constraints, режимы автономии (`yolo` по умолчанию, ADR-0010), конфигурация из `policies/`;
* **Git**: flows A и C (ADR-0003); flow B — вручную через обычный git-review;
* **память**: memory repo + single writer (ADR-0004), чтение памяти в контекст;
* **SQLite**: runs, sessions, messages, tool_calls, approvals, checkpoints, memory_queue, trace;
* **LLM**: один провайдер — OpenAI-compatible API;
* **refuel** по порогу итераций;
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
