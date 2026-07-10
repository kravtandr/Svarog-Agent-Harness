# ADR-0015: Эволюция runtime — hardening безопасности, экономика контекста и исполнение tools

## Статус

Фаза 0 реализована (feat/adr-0015-phase0, коммит e02b2d0, в `main` не влита).
Фаза 1 реализована (feat/adr-0015-phase1, поверх фазы 0, в `main` не влита).
Фазы 2–5 — предложены, не начаты.

| Фаза / пункт | Статус |
|---|---|
| 0.1 path-traversal в skill proposal | ✅ Сделано |
| 0.2 writable `.git` + host-git hooks | ✅ Сделано (Mount-слой — частично, см. оговорку) |
| 0.3 control-plane рядом с workspace | ✅ Сделано (enforcement привязан к docker) |
| 0.4 trust gate / снимок конфига | ✅ Сделано (fail-closed; interactive-approval отложен) |
| 0.5 изоляция конкурентных runs | ✅ Сделано (lease через Run; отдельный worktree — фаза 3) |
| 1.1 per-input метаданные Tool | ✅ Сделано |
| 1.2 персистенция больших tool-результатов | ✅ Сделано |
| 1.3 параллельные read-only батчи | ✅ Сделано (trace-запись последовательна, см. оговорку) |
| 1.4 микрокомпакция | ✅ Сделано |
| 1.5 лимиты индекса памяти | ✅ Сделано |
| 1.6 детектор затухающей отдачи | ✅ Сделано |
| Фаза 2 — deferred-схемы tools | ❌ Не начато |
| Фаза 3 — child runs | ❌ Не начато |
| Фаза 4 — rg-backed coding tools | ❌ Не начато |
| Фаза 5 — ops/UX | ❌ Не начато |

### Реализация фазы 0 — заметки о scoping

* **0.1** — `svarog_harness.paths.safe_join` (нейтральный util); валидация имени
  скилла и ключей `files` в `validate_proposal`; defense-in-depth `safe_join`
  на записи в `SkillRepoFlow.create_proposal`. Reproducer'ы:
  `tests/test_skill_governance.py`, `tests/test_file_tools.py`.
* **0.2** — три слоя: (Tool) denylist `.git`/`.svarog` на запись в
  `resolve_in_workspace`; (Host git) hardened env/флаги на каждом `GitRepo._git`
  (`hooksPath=/dev/null`, global/system-config → `/dev/null`); (Mount)
  `sandbox.separate_git_dir` + `GitRepo.init(separate_git_dir=…)`. **Оговорка:**
  Mount-слой применяется к репозиториям, которые инициализирует сам харнесс
  (memory/skills/tenant-provision); предсуществующее пользовательское
  workspace-дерево харнесс не ре-инициализирует, для него защита держится на
  Tool+Host-git слоях. Reproducer'ы: `tests/test_workspace_flow.py`,
  `tests/test_file_tools.py`.
* **0.3** — `assert_workspace_isolated`. Enforcement привязан к модели изоляции:
  в `docker` (все `standard`-тенанты заклампаны сюда; их раскладка уже disjoint
  через `resolve_tenant_config`) пересечение control-plane с workspace — ошибка
  и run отклоняется; в `local-trusted` bash работает на хосте и путями не заперт
  в принципе — остаточный доступ принят как явный trade-off режима «trusted»
  (§17), нарушение возвращается предупреждением, а не блокирует. Reproducer'ы:
  `tests/test_tenant.py`.
* **0.4** — снимок security-конфига (`runtime.config_snapshot.config_digest`) в
  `Run.meta` на старте; `resume` сверяет и fail-closed при расхождении
  (`ConfigDriftError`). Reproducer: `tests/test_cli_run_traces.py`.
* **0.5** — per-workspace lease через `Run.workspace` + `Run.heartbeat_at`
  (миграция `d4b7f2a9c1e5`); `acquire_workspace_lease` отклоняет второй живой
  run; `recover_interrupted_runs` опирается на протухший heartbeat, а не на
  голое RUNNING. Reproducer'ы: `tests/test_resume.py`.

### Реализация фазы 1 — заметки о scoping

* **1.1** — `Tool.is_read_only`/`is_concurrency_safe` с fail-closed дефолтами;
  `True` — только `read_file`, `list_dir`, `search_files`, `read_skill`,
  `read_memory`. Тесты: `tests/test_tools_base.py`, `tests/test_file_tools.py`.
* **1.2** — spill в `loop._render_tool_result` (redaction → персистенция →
  усечение) в `.svarog/tool-results/<run8>/<call>.txt`; `read_file` из spill
  исключён (петля Read→файл→Read) и получил `offset`/`limit` (часть фазы 4 —
  дочитывание частями); потолок захвата bash поднят до ~1 МБ. **Оговорка:**
  вместо `.gitignore` пользователя `.svarog/` уходит в `info/exclude`
  git-каталога (`GitRepo.ensure_excluded` в `commit_step`) — рабочее дерево
  пользователя не трогается, при separate-git-dir exclude лежит вне bind-mount.
  Тесты: `tests/test_loop.py`, `tests/test_workspace_flow.py`, `tests/test_shell.py`.
* **1.3** — `_concurrency_safe_prefix` + `_execute_batch` в loop; policy
  оценивается до партиционирования в исходном порядке; NOTIFY консервативно
  не батчится (батч — только ALLOW). **Оговорка:** параллелится только
  `tool.call` — trace-запись остаётся последовательной (recorder держит одну
  SQLite-сессию, конкурентный доступ к ней небезопасен). Тесты: `tests/test_loop.py`
  (параллельность доказана «спаренным» tool, порядок, один checkpoint на батч).
* **1.4** — `LoopState.last_prompt_tokens` как триггер; очистка — только
  content tool-сообщений старше защищённого хвоста и длиннее 500 символов;
  маркер ссылается на spill-файл из 1.2, если он есть. Тесты: `tests/test_loop.py`.
* **1.5** — потолок при генерации: `wiki.render_index(max_lines=…)` + строка
  ≤ 200 символов; страховка при чтении: `reader.read_memory` режет по границе
  строки с warning-рецептом. `memory.index_max_lines` прокинут через
  `MemoryWriter`. Тесты: `tests/test_memory_wiki.py`, `tests/test_memory.py`.
* **1.6** — счётчики в `LoopState` (сигнатура = sha256(name, arguments_json,
  результат)); исход — `suspended` с человекочитаемой причиной; счётчики
  сбрасываются при suspend, чтобы resume получил свежее окно. Тесты:
  `tests/test_loop.py`.

## Контекст

Два независимых разбора против референсной реализации Claude Code
(`~/reference/fast-code`, далее CC) дали два непересекающихся пласта находок:

* **экономика контекста и исполнение tools** — систематический разрыв: между
  «вся история в контексте» и «полный сброс через refuel» у Svarog нет
  промежуточных слоёв, tool-выводы теряются при обрезке, читающие вызовы
  исполняются последовательно, зацикливание ловится поздно;
* **безопасность исполнения** — взгляд «глазами атакующего» вскрыл несколько
  путей записи/исполнения на хосте из-под агента, которые анализ «что
  перенять» структурно не видит.

Находки безопасности проверены по коду и подтверждены (ссылки — в фазе 0).
Они первичны: пока агент может выйти за пределы sandbox, улучшать
эффективность контекста преждевременно.

Переносим **механики, не архитектуру**: у CC компакция вплетена в
интерактивный UI, feature-флаги и prompt-cache-editing Anthropic API; его
permissions — императивный helper с fail-open по умолчанию; его ядро — огромный
UI-зависимый `ToolUseContext`. Svarog берёт детерминированные срезы,
совместимые с resumable-runs (ADR-0005), «enforcement over classification»
(ADR-0002) и неотключаемым critical-набором (ADR-0010). Что **не** берём:
Anthropic-coupling, `bypassPermissions`, optional/fail-open sandbox, raw host
hooks, marketplace без pin/review, mutable JSONL как источник истины,
React/Ink в runtime API.

---

## Фаза 0 — P0 security hardening (блокирующая) — ✅ СДЕЛАНО

Пять путей эскалации из-под агента на хост. Первые два дают host-side code
execution и чинятся точечно — их закрываем прежде всего.

### 0.1 Path traversal в skill proposal → произвольная запись на хосте — ✅

**Проблема (подтверждена).** `create_skill_proposal` принимает `name` и словарь
`files` (`tools/skill_tools.py:49`); `validate_proposal` проверяет только
контент `SKILL.md`, совпадение имени и provenance — **пути не валидирует**
(`skills/proposal.py:46`). Затем `SkillRepoFlow.create_proposal` пишет
`target = self._repo.path / request.skill_name / rel` напрямую
(`gitflow/skill_repo.py:54`), где `rel` — ключ из `files`. Ни `skill_name`, ни
`rel` не чистятся: `files={"../../../.ssh/authorized_keys": "..."}` или
`skill_name="../.."` уводят запись за пределы skills-репозитория. Запись
происходит **host-side, на этапе создания proposal, до ревью и вне репозитория**
— поэтому `git diff` ревьюеру её не покажет (Flow B здесь не защищает). Контраст:
`file_tools.resolve_in_workspace` (`tools/file_tools.py:21`) делает ровно
правильную защиту — в skill_repo её просто нет.

**Решение.**
* Вынести `resolve_in_workspace` в нейтральный util `paths.safe_join(root, rel)`:
  запрет absolute, `resolve()` (раскрывает `..` и symlink), обязательный
  `is_relative_to(root.resolve())`.
* `validate_proposal` — ранняя валидация (ошибка возвращается модели): имя
  скилла по `^[a-z0-9][a-z0-9._-]*$` без `..`/`/`/начальной точки; каждый
  ключ `files` пропускается через `safe_join(skill_name_dir, rel)` вхолостую.
* `SkillRepoFlow.create_proposal` — defense-in-depth: тот же `safe_join`
  на записи, отказ = исключение (proposal не материализуется). Валидатор и
  writer не доверяют друг другу.

### 0.2 Writable `.git` + host-git без отключённых hooks → escape из sandbox — ✅

**Проблема (подтверждена).** В контейнер монтируется весь workspace как `rw`,
включая `.git` (`sandbox/docker.py:102`). `file_tools._SKIP_DIRS` исключает
`.git` только из **поиска/листинга**, но `write_file` с путём
`.git/hooks/pre-commit` проходит `resolve_in_workspace` (он формально внутри
workspace) и пишется. Затем host-side commit исполняет обычный `git -C`
(`gitflow/repo.py:20`) **без** отключения hooks, global/system-config и filters.
Итог: агент сажает hook изнутри sandbox — тот исполняется на хосте при
автокоммите Flow C. Контейнеру `.git` при этом вообще не нужен: коммитит хост.

**Решение (три слоя).**
* **Mount:** объектное хранилище git выносится за пределы монтируемого пути
  (`git init --separate-git-dir=<agent-home>/.gitdirs/<ws>`; в workspace
  остаётся файл-указатель `.git`). Даже rw-workspace из sandbox не достаёт до
  hooks/config — они физически вне bind-mount.
* **Tool:** `resolve_in_workspace` получает denylist префиксов
  (`.git`, `.svarog`) — запись/правка под ними отвергается как ошибка tool
  (сейчас denylist влияет только на поиск). Инвариант симметричен чтению.
* **Host git:** все вызовы `GitRepo._git` — с жёстким окружением и флагами:
  `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`,
  `-c core.hooksPath=/dev/null -c core.fsmonitor=false`, нейтрализация
  clean/smudge-filters. Эталон — защита Git admin paths и bare-repo planting
  в CC (`sandbox-adapter.ts:257`).

### 0.3 Control-plane рядом с agent-writable деревом — ✅

**Проблема (уточнённая).** Scaffold кладёт `memory/`, `skills/`, `svarog.db`,
`svarog.yaml` в agent-home (`scaffold.py:55`). Sandbox монтирует **только**
`self.workspace`, а не весь home, поэтому из docker-bash control-plane
недостижим. Но в режиме `local-trusted` (без изоляции, §17) bash работает на
хосте и — в отличие от file_tools — путями не заперт: может обойти
single-writer памяти, skill governance и тронуть trace/approval-БД. Угроза
реальна конкретно для `local-trusted` + `bash`, не универсально.

**Решение.**
* Инвариант раскладки: workspace — строго нижележащий и **непересекающийся** с
  control-plane каталог (`workspaces/tasks/<run>/`), никогда не корень home.
  Scaffold и docs это фиксируют; при старте run проверяется, что
  DB/memory/skills не лежат внутри и не являются предками workspace.
* Control-plane доступен агенту только через узкие capability-интерфейсы
  (память — `remember`, скиллы — proposal, trace — recorder) — это уже так;
  задача — гарантировать, что **другого** пути (сырой файловый/bash-доступ) нет.
* Остаточный риск `local-trusted`+`bash` документируется как явный trade-off
  режima «trusted» (§17); для standard-роли он снят клампом в docker (ADR-0013).

### 0.4 Trust gate и заморозка конфигурации на run — ✅

**Проблема (подтверждена).** Project-config перекрывает user-config
(`config/loader.py:62`) и способен объявить host-side stdio-MCP; resume
перечитывает изменяемый конфиг заново (`runtime/orchestrator.py:352`). Autonomy
и tenant-role уже заморожены в run (ADR-0010/0013) и переклампываются на resume
— но провайдер, MCP-серверы и policy-правила подтягиваются из mutable-yaml на
каждом resume.

**Решение.**
* На старте run вычисляется хеш **effective**-конфига (провайдер, MCP-серверы,
  policy-правила, secrets-refs) и сохраняется snapshot в записи run.
* resume работает от snapshot'а, а не от свежего чтения yaml. Расхождение
  текущего конфига со snapshot → `require_approval`; headless — **fail-closed**
  (resume отклоняется, а не тихо берёт новый конфиг).
* Секреты — краткоживущая capability на время вызова, а не постоянно в
  окружении процесса/sandbox (follow-on внутри фазы; сейчас — inject-список,
  ADR-0006).

### 0.5 Изоляция конкурентных runs — ✅

**Проблема (подтверждена).** Gateway способен запустить несколько run'ов на
общем workspace; recovery переводит **все** RUNNING→SUSPENDED без
per-workspace различения (`trace/recorder.py:310` — коммент честно признаёт
ложную приостановку чужого активного run'а как принятый до server-режимов
компромисс). Single-writer есть у памяти, у workspace/run — нет.

**Решение.**
* Per-workspace lease при старте run: `owner` + `heartbeat`; gateway отказывает
  во втором run'е на залоченном workspace.
* `recover_interrupted_runs` опирается на протухший heartbeat, а не на голое
  состояние RUNNING — живой run в другом процессе не приостанавливается ложно.
* Сильнее (смыкается с фазой 3): отдельный git-worktree на каждый run —
  физическая изоляция рабочих деревьев.

---

## Фаза 1 — Экономика контекста — ✅ СДЕЛАНО

Детерминированные слои (код, не LLM) между «всё в контексте» и refuel.
Порядок внутри фазы: 1.1 (фундамент) → 1.2 → 1.3 → остальное.

### 1.1 Per-input метаданные Tool с fail-closed дефолтами

Фундамент для 1.3. В `Tool` (`tools/base.py`) — два метода с безопасными
дефолтами (паттерн `buildTool` CC — «не знаешь → считай худшее»):

```python
def is_read_only(self, args: ArgsT) -> bool:
    return False          # fail-closed: по умолчанию пишет
def is_concurrency_safe(self, args: ArgsT) -> bool:
    return self.is_read_only(args)
```

`True` переопределяют только заведомо читающие: `read_file`, `list_dir`,
`search_files`, `read_skill`, `read_memory`. **`bash` — всегда `False`**: CC
парсит команду shell-quote'ом и классифицирует, Svarog сознательно не строит
классификацию поверх исполнения (ADR-0002); цена консервативности здесь ноль.
MCP-tools — `False` (чужой код). Это ось **исполнения**, не policy:
`risk_level`/`action_type` не трогаются, Policy Engine эти методы не читает
(как `isReadOnly` ≠ permissions у CC). Дефолт-deny сохраняется — забытый
override делает tool медленнее, не опаснее.

### 1.2 Персистенция больших tool-результатов вместо обрезки

`truncate_text` перестаёт терять данные. Backpressure переезжает из tools в
loop — единственное место, где видны и полный вывод, и run, и redaction:

* tools возвращают вывод целиком (жёсткий потолок захвата ~1 МБ остаётся как
  защита памяти процесса);
* `loop._render_tool_result`: порядок **redaction → персистенция → усечение**.
  Если после redaction текст длиннее `tool_output_context_chars` (default
  20 000), полный текст пишется в
  `<workspace>/.svarog/tool-results/<run_id8>/<call_id>.txt`, модель получает
  голову + маркер `[показано M из N символов; полный вывод: <путь> — читай
  read_file частями]`;
* персистится **отредактированный** текст — секреты не попадают на диск
  (ADR-0006);
* файл внутри workspace → `read_file` достаёт его и на хосте, и в sandbox
  (тот же bind-mount);
* `.svarog/` в `.gitignore` (Flow C его не коммитит; secret-scan рабочего
  дерева видит — сетка поверх redaction). Согласуется с denylist из 0.2.

`read_file`-результаты не персистятся (петля «Read → файл → Read»); для них —
честная обрезка с указанием offset/limit.

### 1.3 Параллельное исполнение read-only батчей (требует 1.1)

`_execute_pending` партиционирует `pending_tool_calls` (образец
`partitionToolCalls` CC): подряд идущие вызовы, у которых **и** policy-решение
`ALLOW`, **и** `is_concurrency_safe(args)`, собираются в батч и исполняются
`asyncio.gather` (потолок `max_tool_concurrency`, default 4); остальное — по
одному, как сейчас. Совместимость с write-ahead (ADR-0005):

* policy оценивается **до** партиционирования, последовательно, в исходном
  порядке — `require_approval`/`deny`/`notify` не «уезжают» в параллельный
  батч; approval-вызов прерывает исполнение там же, где сейчас;
* результаты дописываются в `state.messages` в исходном порядке; checkpoint —
  один на батч;
* падение посреди батча → resume переисполняет батч целиком; безопасно, т.к.
  в батче только читающие вызовы. Инвариант «мутирующий вызов исполняется не
  более одного раза после фиксации» не ослабляется;
* побочно: нагрузка на SQLite от checkpoint'ов падает пропорционально батчам.

### 1.4 Микрокомпакция: очистка старых tool-результатов

Дешёвый слой без LLM. Перед вызовом провайдера, если `prompt_tokens`
последнего ответа превысил `microcompact_threshold_ratio * max_context_tokens`
(default 0.6), loop заменяет **содержимое** старых `tool`-сообщений маркером:

```text
[результат инструмента очищен для экономии контекста: <tool>, N символов.
Полный вывод: <путь>  |  либо: повтори вызов при необходимости]
```

Правила (код): защищённый хвост `microcompact_keep_recent` (default 5)
результатов не трогается; сообщения < 500 символов не чистятся; структура
истории сохраняется (`role="tool"`/`tool_call_id` на месте, меняется только
`content`) — provider-совместимость цела; есть файл из 1.2 → маркер ссылается
на него (данные не теряются), иначе предлагает повтор; очищенная история идёт
в checkpoint как есть (ADR-0005 не меняется); полные результаты уже в trace на
момент исполнения — аудит цел. Refuel остаётся глубоким сбросом поверх
микрокомпакции. `_budget_exceeded` по `max_context_tokens` — последний
стоп-кран. Цена: разовая инвалидация префиксного кэша провайдера на
срабатывание (cache-aware-редактирование CC на openai-compatible недоступно —
принимаем; порог 0.6 держит число срабатываний за run в районе 0–2).

### 1.5 Лимиты индекса памяти с самообъясняющим warning

`memory/reader.py` режет по 16 КБ, заменяя файл заглушкой без причины и лечения.
Образец — `truncateEntrypointContent` CC (лимит + warning с рецептом):

* **запись (основное):** `wiki.render_index` — индекс автогенный (ADR-0011),
  потолок обеспечивается при генерации: строка индекса обрезается ~200 символами
  (длинный `summary` укорачивается `…`), `index.md` — по `memory.index_max_lines`
  (default 200) с хвостом `> …и ещё N страниц — см. read_memory`;
* **чтение (страховка):** усечение в `reader.read_memory` режет по границе
  строки и дописывает warning с **действием**: `> WARNING: index.md превысил
  лимит — загружена часть. Сокращай summary; детали переноси в notes.md`.

### 1.6 Детектор затухающей отдачи

Абсолютные стоп-краны дополняются ранним детектором стагнации (идея
`tokenBudget.ts` CC). Loop ведёт скользящее окно и уводит run в `suspended`
(не `failed` — решает человек) при любом сигнале:

* **повтор вызова:** `stagnation_repeats` (default 3) подряд идентичных tool
  calls — совпадают `(name, arguments_json)` **и** результат;
* **затухание токенов:** `stagnation_repeats` итераций с дельтой полезного
  вывода < 500 токенов при отсутствии новых успешных tool-результатов.

Оба детерминированы (счётчики, не LLM-судья). Причина в `suspended`
формулируется для человека: «затухающая отдача: 3 идентичных вызова
search_files без прогресса; resume после уточнения задачи». Смягчение ложных
срабатываний (поллинг через bash): вызовы с разным выводом не идентичны.

---

## Фаза 2 — Отложенная загрузка схем tools — ❌ НЕ НАЧАТО

Progressive disclosure (как для скиллов §3.4 и памяти ADR-0011) — на
tool-схемы; provider-neutral аналог ToolSearch CC, **без** Anthropic beta API.

* `ToolRegistry` делит tools на **core** (полная схема всегда) и **deferred**
  (в промпт — строка `имя — однострочное назначение`);
* новый tool `load_tool(name)` переводит deferred в загруженные; со следующей
  итерации его схема входит в `definitions()`;
* множество загруженных имён — в `LoopState`, сериализуется в checkpoint
  (ADR-0005);
* deferred по умолчанию — только MCP-tools (`mcp.defer_schemas`): именно
  discovery приносит неконтролируемое число схем; встроенных мало.

Гейт включения: 15+ MCP-tools в конфигурации; до того флаг выключен.

---

## Фаза 3 — Child runs вместо in-process subagents — ❌ НЕ НАЧАТО

У Svarog subagents нет — greenfield. Полезное из `AgentTool` CC (lifecycle,
отдельный trace, cancellation, mailbox, worktree isolation) реализуется через
**существующие** сущности, а не новый UI-зависимый механизм:

* дочерний run — обычный `Run` с `parent_run_id`, **своим** budget/policy-
  snapshot (из фазы 0.4) и checkpoint'ом (ADR-0005);
* изоляция — отдельный git-worktree на дочерний run (смыкается с 0.5);
* связь parent↔child — durable-очередь поверх той же SQLite (не in-memory
  mailbox), чтобы переживала падение процесса;
* cancellation/бюджеты наследуются от родителя, но клампятся вниз, не вверх
  (как autonomy/role).

Первый tool — `spawn_child_run(task, budget)`; результат забирается из trace
дочернего run'а.

---

## Фаза 4 — Coding tools — ❌ НЕ НАЧАТО

* заменить Python-обход в `search_files` на `ripgrep`-backend (скорость,
  корректность игнора) с явным `read_range`/пагинацией и честным маркером
  усечения (полный результат — в artifact через 1.2);
* `read_file` получает `offset`/`limit` (уже частично есть для дочитывания
  spill-файлов);
* LSP — следующим этапом как **optional plugin**, не в ядре.

---

## Фаза 5 — Ops/UX (вне runtime API) — ❌ НЕ НАЧАТО

Не трогает ядро; React/Ink в runtime не попадает:

* resume/fork/rename/search сессий; turn-level git rewind;
* JSON/NDJSON-вывод CLI (машиночитаемый); `svarog doctor`;
* cost/context-индикаторы;
* экспорт canonical trace в OpenTelemetry (SQLite остаётся источником истины,
  OTel — производный экспорт, не замена).

---

## Конфигурация

```yaml
runtime:
  microcompact_threshold_ratio: 0.6   # 1.4; доля max_context_tokens
  microcompact_keep_recent: 5         # 1.4; защищённый хвост
  tool_output_context_chars: 20000    # 1.2; порог персистенции
  max_tool_concurrency: 4             # 1.3
  stagnation_repeats: 3               # 1.6
memory:
  index_max_lines: 200                # 1.5
mcp:
  defer_schemas: false                # 2; второй эшелон
sandbox:
  separate_git_dir: true              # 0.2; git-объекты вне mount
```

Все поля — с дефолтами, `extra="forbid"` как везде (§13). Snapshot effective-
конфига (0.4) и per-workspace lease (0.5) — не yaml-поля, а состояние run/БД.

**Реально в схеме (фазы 0–1):** `sandbox.separate_git_dir`,
`runtime.microcompact_threshold_ratio`, `runtime.microcompact_keep_recent`,
`runtime.tool_output_context_chars`, `runtime.max_tool_concurrency`,
`runtime.stagnation_repeats`, `memory.index_max_lines`. Из перечисленного НЕ
добавлен только `mcp.defer_schemas` — он из фазы 2 и приземляется с ней.

---

## Порядок реализации (сводно)

| Фаза | Что | Гейт | Статус |
|---|---|---|---|
| **0** | Security: 0.1 traversal → 0.2 git/mount → 0.5 lease → 0.4 config-snapshot → 0.3 раскладка | **блокирует всё** | ✅ Сделано |
| 1 | 1.1 метаданные → 1.2 spill → 1.3 параллель → 1.4 микрокомпакция → 1.5 индекс → 1.6 стагнация | после 0 | ✅ Сделано |
| 2 | Deferred-схемы | 15+ MCP-tools | ❌ Не начато |
| 3 | Child runs (`parent_run_id`) | после 1 | ❌ Не начато |
| 4 | rg-backed coding tools | после 1 | ❌ Не начато |
| 5 | Ops/UX | параллельно, вне ядра | ❌ Не начато |

Внутри фазы 0 первыми идут 0.1 и 0.2 — они дают host-side code execution и
чинятся точечно; 0.3–0.5 архитектурнее, но обязательны до server/gateway-
нагрузки.

## Последствия

* **Безопасность (фаза 0):** закрываются два пути host-side code execution
  (skill-traversal, git-hook planting) и три архитектурных пробела (раскладка,
  trust-gate, изоляция runs). До этого — никаких новых возможностей: они
  расширяют поверхность атаки. Каждая находка фазы 0 сопровождается
  тестом-репродьюсером (запись за skill-root; `.git/hooks` через write_file;
  resume с подменённым MCP; второй run на залоченном workspace).
* **Контекст (фаза 1):** длинные run'ы перестают упираться в
  `max_context_tokens` задолго до refuel — контекст деградирует управляемо, а
  не останавливается. `suspended` по контексту становится редким исключением.
* **Данные не теряются:** усечённое лежит в `.svarog/tool-results/` и
  досягаемо через `read_file`. Появляется накапливающийся мусор в workspace —
  уборка (при `verify`/завершении run) остаётся долгом, зафиксирована здесь.
* **Исполнение:** батчи чтения ускоряются на I/O-bound задачах и снижают
  частоту checkpoint'ов; семантика write-ahead для мутирующих вызовов не
  меняется. Новый класс поведения — переисполнение read-only батча при resume —
  безопасен, но покрывается тестом на идемпотентность.
* **`Tool`** получает вторую ось метаданных (исполнение) рядом с policy-осью;
  fail-closed дефолты купируют риск перепутать оси.
* **Стоимость:** микрокомпакция периодически инвалидирует префиксный кэш
  провайдера (для локальных моделей бесплатно; для платных — разово против
  роста каждого следующего запроса). Порог 0.6 держит число срабатываний малым.
* **Стагнация:** возможны ложные срабатывания на легитимных повторах; смягчено
  сравнением результата, конфигурируемым порогом и исходом `suspended` (не
  потеря run'а).
* **trace** остаётся полным (пишется до компакции/усечения) — маркеры в
  контексте не влияют на аудит и `svarog trace`.
* Механики 1.2/1.4/1.6 — детерминированный код без LLM: воспроизводимы в
  тестах и S-симуляциях, не добавляют вызовов модели в критический путь.
* **Осознанно отложено:** LLM-суммаризация при refuel (autocompact-аналог CC).
  Плюс — богаче task_state, чем механический хвост (`refuel.py`); минус —
  галлюцинации в критическом пути resume и стоимость. Компромисс на будущее:
  LLM-суммаризация с механическим fallback при её отказе. Отдельное решение,
  не входит в этот ADR.
