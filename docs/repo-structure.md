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
      main.py               # init, run, chat, skills, traces, approvals
      approval_ui.py        # интерактивный approval в терминале

    config/                 # svarog.yaml → pydantic-settings
      schema.py
      loader.py
      paths.py              # разрешение skills/memory-путей (общее для cli/gateway)

    runtime/                # ядро (§6.2)
      loop.py               # agent loop: state machine, итерации
      orchestrator.py       # TaskRunner: один прогон задачи под RunHooks (cli/gateway/telegram)
      checkpoint.py         # сериализация/восстановление (ADR-0005)
      context_builder.py    # слои контекста, budget (§6.3)
      refuel.py             # task_state.md, пересборка контекста (§6.10)
      verifier.py           # запуск checks, приоритет детерминированных (§6.11)

    llm/
      provider.py           # интерфейс ModelProvider
      openai_compatible.py  # единственная реализация в MVP

    tools/                  # (§6.5)
      base.py               # Tool: schema, risk_level, timeout, sandbox_requirement
      registry.py
      router.py             # вызов через Policy Engine → executor
      file_tools.py         # read/write/edit/list/search
      shell.py              # bash
      git_tool.py           # git-операции агента (без credentials)
      skill_tools.py        # read_skill, create_skill_proposal
      user_tools.py         # ask_user, request_approval

    policy/                 # (§6.6, ADR-0002)
      engine.py             # allow / deny / require_approval + constraints
      rules.py              # загрузка policies/*.yaml
      bash_heuristics.py    # слой 2: паттерны для UX-классификации bash
      approvals.py          # жизненный цикл ApprovalRequest/Decision

    sandbox/                # (§6.9, ADR-0002)
      base.py               # интерфейс SandboxBackend
      docker_backend.py     # network off, non-root, limits, mounts
      local_trusted.py      # исполнение без изоляции (явный режим)

    memory/                 # (§6.7, ADR-0004)
      manager.py            # чтение памяти, retrieval в контекст
      change_request.py     # MemoryChangeRequest
      writer.py             # single writer + очередь

    gitflow/                # (ADR-0003)
      repo.py               # subprocess-обертка над git
      commit_gate.py        # обязательный secret scan перед commit (все flow)
      skill_repo.py         # Flow B: proposal-ветка, diff, merge/reject (§18)
      workspace.py          # Flow C: pull/branch/commit/push-with-approval

    skills/                 # (§6.4)
      loader.py             # сканирование, SKILL.md frontmatter, skill cards
      models.py             # Skill, SkillMetadata
      proposal.py           # SkillProposalRequest + валидация (Flow B, §18)
      proposal_manager.py   # governance-flow: persist/merge/reject proposals
      curator/              # пост-MVP (ADR-0009)
        pruning.py          # слой 1: механический
        consolidation.py    # слой 2: LLM

    secrets/                # (ADR-0006)
      store.py              # интерфейс SecretStore
      file_store.py         # MVP: шифрованный файл + env
      redaction.py

    storage/                # (ADR-0007)
      db.py                 # SQLAlchemy: engine, session
      models.py             # runs, messages, tool_calls, approvals, checkpoints…
      migrations/           # Alembic
      queue.py              # QueueBackend: sqlite | (redis позже)
      events.py             # EventStream: in-process | (redis позже)

    trace/                  # (§6.12, §15)
      recorder.py           # запись всех сущностей аудита
      viewer.py             # traces list/show для CLI

    gateway/                # внешние интерфейсы (пост-MVP M5, §10.2/§10.4)
      service.py            # GatewayService: фоновые runs + стриминг событий
      api.py                # FastAPI create_app: REST + WebSocket
      models.py             # pydantic-схемы запросов/ответов
      telegram.py           # Telegram-бот: long-polling, approval-кнопки

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
