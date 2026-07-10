# Структура репозитория Svarog

Структура исходников платформы (не путать с agent-home репозиторием пользователя — его структура описана в §8 TASK.md).

```text
svarog/
  pyproject.toml            # uv + hatchling, зависимости, ruff/mypy/pytest конфиг
  README.md
  TASK.md                   # ТЗ
  docs/
    adr/                    # ADR-0001…0010
    repo-structure.md
    first-issues.md

  src/svarog_harness/
    __init__.py
    cli/                    # Typer-приложение
      main.py               # все команды + интерактивный approval в терминале (одним модулем)

    config/                 # svarog.yaml → pydantic-settings
      schema.py
      loader.py
      paths.py              # разрешение skills/memory-путей (общее для cli/gateway)

    runtime/                # ядро (§6.2)
      loop.py               # agent loop: state machine, итерации, _execute_tool через Policy Engine
      orchestrator.py       # TaskRunner: один прогон задачи под RunHooks (cli/gateway/telegram)
      checkpoint.py         # LoopState: сериализация/восстановление (ADR-0005)
      context_builder.py    # слои контекста, budget (§6.3)
      refuel.py             # task_state.md, пересборка контекста (§6.10)

    verifier/               # (§6.11)
      runner.py             # запуск checks, приоритет детерминированных, secret scan дерева

    llm/
      provider.py           # интерфейс ModelProvider + типы (ChatMessage, ToolCallRequest…)
      openai_compatible.py  # единственная реализация в MVP
      tool_call_leak.py     # фолбэк-парсер tool call'ов, протёкших в текст

    tools/                  # (§6.5)
      base.py               # Tool: args_model, risk_level, timeout, sandbox_requirement
      registry.py           # ToolRegistry
      file_tools.py         # read/write/edit/list/search
      shell.py              # bash
      skill_tools.py        # read_skill, create_skill_proposal
      memory_tools.py       # remember
      approval.py           # request_approval
      user_tools.py         # ask_user (вопрос человеку с таймаутом, §6.5)
      # оркестрация вызова (policy → sandbox/host → recorder) — в runtime/loop.py:_execute_tool,
      # отдельного router.py нет; git — не tool агента, а host-flow (gitflow/, ADR-0002)

    policy/                 # (§6.6, ADR-0002)
      engine.py             # allow / notify / deny / require_approval + critical-набор; жизненный цикл approval — в engine + recorder
      rules.py              # загрузка policies/*.yaml
      heuristics.py         # слой 2: bash-паттерны для UX-эскалации (только LOW/MED → HIGH)

    sandbox/                # (§6.9, ADR-0002)
      base.py               # интерфейс ExecutionEnvironment
      docker.py             # network off, non-root, cap-drop, limits, mounts
      local.py              # local-trusted: исполнение без изоляции (явный режим)
      factory.py            # выбор backend по SandboxConfig

    memory/                 # (§6.7, ADR-0004)
      reader.py             # read_memory: чтение памяти в контекст (user→projects→decisions)
      change.py             # MemoryChangeRequest + операции (create/append/replace_section/delete)
      apply.py              # применение заявки к файлам memory-репо
      writer.py             # single writer: drain очереди memory_queue → commit_guarded

    gitflow/                # (ADR-0003)
      repo.py               # subprocess-обертка над git
      commit_gate.py        # обязательный secret scan перед commit (все flow)
      skill_repo.py         # Flow B: proposal-ветка, diff, merge/reject (§18)
      workspace.py          # Flow C: pull/branch/commit/push-with-approval

    skills/                 # (§6.4)
      loader.py             # сканирование, SKILL.md, skill cards
      frontmatter.py        # разбор YAML-frontmatter SKILL.md
      models.py             # Skill, SkillMetadata (provenance human|agent)
      proposal.py           # SkillProposalRequest + валидация (Flow B, §18)
      proposal_manager.py   # governance-flow: persist/merge/reject proposals
      curator/              # Skill Curator (ADR-0009, §18.1)
        state.py            # CuratorStore: lifecycle-состояние скиллов в SQLite
        pruning.py          # слой 1: механические lifecycle-переходы
        consolidation.py    # слой 2: LLM (пост-#28)

    secrets/                # (ADR-0006)
      store.py              # SecretStore + File/Env/Layered: JSON 0600 (без шифрования) + env fallback
      denylist.py           # пути-секреты (.env, *.key, …) для write_file и коммитов
      scanner.py            # pre-commit secret scan: паттерны + entropy + known_values
      redaction.py          # вырезание известных значений из trace/контекста

    storage/                # (ADR-0007)
      db.py                 # SQLAlchemy: engine, session
      models.py             # runs, messages, tool_calls, approvals, checkpoints, memory_queue…
      migrations/           # Alembic
      events.py             # EventStream: in-process pub/sub | (redis позже)
      locks.py              # LockBackend + FileLockBackend (flock): сериализация memory-writer
      # QueueBackend отдельным модулем нет — очередь памяти = таблица memory_queue

    trace/                  # (§6.12, §15)
      recorder.py           # единственный писатель в БД: запись всех сущностей аудита
      viewer.py             # traces list/show для CLI
      lookup.py             # поиск run/approval по префиксу id

    gateway/                # внешние интерфейсы (пост-MVP M5, §10.2/§10.4)
      service.py            # GatewayService: фоновые runs + стриминг событий
      api.py                # FastAPI create_app: REST + WebSocket
      models.py             # pydantic-схемы запросов/ответов
      telegram.py           # Telegram-бот: long-polling, approval-кнопки

    mcp/                    # MCP-интеграция (пост-MVP M5, §9)
      models.py             # MCPToolSpec, MCPBackend
      tool.py               # MCPTool: MCP-инструмент как Tool
      integration.py        # connect_mcp_servers (stdio SDK), build_mcp_tools

  skills/                   # official skills (§23), поставляются с платформой
    git-workflow/
    skill-authoring/
    skill-curator/          # пост-MVP
    ...

  evals/                    # исполняемые сценарии из §26
    scenarios/
    conftest.py

  tests/                    # зеркалирует src/svarog_harness/
```

## Принципы

* **Зависимости направлены вниз**: `cli` → `runtime` → (`tools`, `policy`, `sandbox`, `memory`, `gitflow`, `skills`, `llm`) → `storage`/`trace`. Никаких импортов из `cli` в ядро.
* **Каждый pluggable-интерфейс** (ModelProvider, SandboxBackend, QueueBackend, SecretStore) живет в `base.py`/`provider.py` своего пакета; реализации — соседние модули.
* **`gateway/` — первый внешний интерфейс (M5)**: `runtime` общается с внешним миром только через `RunHooks` оркестратора, события (`storage/events.py`) и approvals, поэтому REST/WS/Telegram подключены без изменений ядра. CLI и gateway гоняют один `TaskRunner`.
* **`skills/` в корне** — это контент, не код: официальные скиллы копируются в agent-home при `svarog init`.
