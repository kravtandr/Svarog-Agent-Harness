# Первые issues

Порядок — топологический: каждый milestone дает работающий срез. Оценки нет намеренно — приоритет и зависимости важнее.

## M0 — Bootstrap

1. **Scaffold репозитория**: pyproject (uv, hatchling), src-layout по `docs/repo-structure.md`, ruff + mypy strict + pytest, CI (lint, typecheck, tests), LICENSE (Apache-2.0).
2. **Конфигурация**: схема `svarog.yaml` в pydantic-settings, загрузка project + user уровней, валидация с понятными ошибками (§13).
3. **Схема БД + миграции**: SQLAlchemy-модели (sessions, runs, messages, tool_calls, approvals, checkpoints, memory_queue, skill_loads, check_results, artifacts, error_events — §15), Alembic, автосоздание SQLite при init.

## M1 — Ядро loop (local trusted, без sandbox)

4. **ModelProvider**: интерфейс + openai-compatible реализация, streaming, счет токенов/стоимости, retries/timeouts.
5. **Tool base + registry**: описание tool (schema, risk_level, timeout, sandbox_requirement — §6.5), регистрация, генерация tool definitions для LLM.
6. **File tools**: read_file, write_file, edit_file, list_dir, search_files; пути только внутри workspace.
7. **Bash tool (local trusted)**: исполнение с timeout, capture stdout/stderr; sandbox подключается в M2.
8. **Agent loop v0**: линейный run (без resume) — build context → LLM → tool calls → observe → iterate; стоп по max_iterations/budget; полная запись trace.
9. **CLI `run` + `traces list/show`**: запуск задачи из терминала, просмотр trace.

## M2 — Безопасность и resumability

10. **Docker sandbox backend**: network off, non-root, CPU/RAM limits, timeout, mounts workspace rw / skills ro (ADR-0002); выбор backend по конфигу. Адаптировать `tools/environments/{base,docker,local}.py` из hermes-agent (MIT, см. `docs/reference-analysis.md`) — не писать с нуля.
11. **Policy Engine**: allow/notify/deny/require_approval + execution constraints, режимы автономии (`yolo` по умолчанию, ADR-0010), неотключаемый critical-набор, protected branches, правила из `policies/*.yaml`, bash-эвристики как UX-слой (могут эскалировать до notify, не участвуют в critical) — взять `DANGEROUS_PATTERNS` из hermes `tools/approval.py`; режим автономии замораживается при старте run; deny прямых коммитов в skills/.
12. **Checkpoint/resume**: state machine run'а (ADR-0005), checkpoint после каждой итерации, write-ahead для tool calls, recovery незавершенных runs при старте.
13. **Approval + notify flow**: request_approval tool → run в `waiting_approval` → CLI-команда/промпт решения → resume; approval показывает фактическую команду/diff. Notify-события: немедленное выполнение, заметный вывод в CLI, выделение в trace (ADR-0010).

## M3 — Skills, память, Git

14. **Skill loader**: сканирование `skills/`, парсинг SKILL.md frontmatter (совместимость с agentskills.io; за основу — `skills/_frontmatter.py` из HKUDS, MIT), skill cards в контекст, `read_skill` on-demand, `skills list/check`, логирование SkillLoad (нужно curator'у в M5).
15. **Pre-commit secret scan**: сканер (паттерны ключей + entropy + значения из SecretStore), блокирующий commit во всех git flows; проверка повторяется перед push; `.gitignore` и denylist путей при init (ADR-0006). Делается до flows A/C, т.к. является их гейтом.
16. **Memory: Flow A**: memory repo, MemoryChangeRequest, single writer c очередью в SQLite (ADR-0004), чтение памяти в context builder.
17. **Workspace: Flow C**: pull before work, task branch, commit по шагам, push через approval, diff generation; git push — host-компонентом (ADR-0006).
18. **Refuel**: порог итераций/токенов → task_state.md + commit → новая сессия пересобирает контекст (§6.10).
19. **`svarog init`**: создание agent-home по §8, копирование official skills, дефолтные policies, `.gitignore` для секретов.

## M4 — Качество и завершение MVP

20. **Verifier (детерминированный)**: запуск checks (тесты, линтеры, secret scan) после run'а, приоритет над самооценкой агента; skill-specific checks.
21. **SecretStore + redaction**: file store, named references, инжекция в sandbox env, redaction в trace (ADR-0006).
22. **CLI `chat`**: интерактивная сессия, каждое сообщение — run в общей session.
23. **Evals из §26**: сценарии критерия готовности MVP (ADR-0008) в `evals/`, запуск в CI.

## M5 — Первое расширение (пост-MVP, порядок из ADR-0008)

24. **REST/WebSocket API** (gateway) + async approval через API.
25. **Telegram-интерфейс**: задачи, streaming updates, approval-кнопки.
26. **Skill governance**: create_skill_proposal tool, checks, diff review, merge flow (Flow B автоматизирован).
27. **Skill Curator, слой 1**: автоматические lifecycle-переходы (active/stale/archived) по usage-статистике, pinned, защита scheduled-скиллов, только agent-created (ADR-0009, инварианты hermes).
28. **Skill Curator, слой 2**: LLM-консолидация на auxiliary-модели (opt-in), дубликаты, улучшение skill cards, отчеты; изменения через proposals; `skills curate`.
29. **MCP-интеграция**: подключение серверов, discovery, default `risk: high` + approval.
