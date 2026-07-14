# ADR-0016: Внешний агент как data-plane (Claude Code, Codex, OpenCode, …)

## Статус

Принято; фазы 1-4 реализованы.

| Фаза / пункт | Статус |
|---|---|
| 1. Executor-шов + конфиг (`executor.*`, digest §0.4) | ✅ Сделано |
| 1. Адаптер `claude-code` (stream-json → AgentEvent, golden-тесты) | ✅ Сделано |
| 1. Стриминг sandbox (`ExecutionEnvironment.stream`, docker+local) | ✅ Сделано |
| 1. `ExternalAgentExecutor`: стрим → trace, redaction, heartbeat | ✅ Сделано |
| 1. Fail-closed гейты (docker-only, supervised) | ✅ Сделано |
| 1. Internal-network + relay + LLM-прокси (§2/§3) | ✅ Сделано (bridge на хосте, relay-sidecar в internal-сети; метеринг anthropic/openai, бюджет → 429 → suspended) |
| 1. Agent-state volume (§5) | ✅ Сделано (`<control-plane>/agent-state/<adapter>` → state_dir адаптера) |
| 1. §1.2-персистенция больших tool_result | ✅ Сделано |
| Фаза 2 — MCP-сервер + контекст (§4) | ✅ Сделано (HTTP-MCP на bridge: remember/read_memory/read_skill/create_skill_proposal/ask_user/request_approval; CLAUDE.md/AGENTS.md в state volume) — транспорт HTTP-bridge вместо unix-socket: bind-mount сокетов не работает на Docker Desktop |
| Фаза 3 — cooperative tier (§6/§7) | ✅ Сделано (managed-settings ro-mount, PreToolUse → общий policy-конвейер, decision cache по отпечатку, grace → suspend → waiting_approval, resume с prompt-решением, chat поверх agent-сессий, supervised-гейт) |
| Фаза 3.5 — субагентная делегация (`spawn_child_run` → external) | ✅ Сделано (с полной инфрой bridge у ребёнка) |
| Фаза 4 — адаптеры codex/opencode | ✅ Сделано (golden-JSONL тесты; матрица capabilities: hooks+mcp — только claude-code, поэтому supervised и память/скиллы с codex/opencode fail-closed недоступны) |

**Не покрыто (следующие итерации):** subscription/OAuth-режим прокси
(§3, реализован только api-key); refuel-supervisor для внешних runs в
gateway; `svarog doctor`-гейт версии агента в образе; прогон с настоящим
бинарём Claude Code (нужен образ с агентом и подписка). Контейнерная
топология §2-§4 проверена живым docker-e2e (`tests/test_external_docker.py`):
internal-сеть + relay + bridge/MCP из контейнера + auto-commit + cleanup.

## Контекст

Svarog — control-plane и data-plane в одном: `AgentLoop` (runtime/loop.py)
сам ведёт цикл observe → reason → act, вызывая LLM через `ModelProvider` и
исполняя собственные tools. Reference-analysis сознательно отвергла bridge к
чужим подпискам (Claude Code/Codex) **как замену model-agnostic API-подхода**
(docs/reference-analysis.md).

Однако есть отдельный запрос, который этим решением не закрыт: использовать
зрелый внешний кодинг-агент (Claude Code, Codex CLI, OpenCode) как
**исполнитель** — с его reasoning, встроенными инструментами и подпиской, — но
сохранить backbone Svarog: sandbox-инварианты, Policy Engine/approval,
Git-native память, governance скиллов, мультиарендность, resumable runs и
audit trace. То есть внешний агент = data-plane, Svarog = control-plane.

Ключевая угроза дизайна: два agent loop нельзя «сливать» — внешний агент
не отдаст свой цикл tool-use. Значит, контроль должен строиться не на
перехвате каждого шага (это невозможно гарантировать для произвольного
агента), а на тех же принципах, что и ADR-0002: **enforcement по границе,
LLM (теперь — целый чужой агент) считается недоверенным компонентом**.

## Ключевые наблюдения

1. **Шов уже существует.** `TaskRunner` (orchestrator.py) владеет всем
   control-plane: cfg-клампы по роли, секреты, локи, workspace prep, verifier,
   trace. `AgentLoop` — лишь одна из вещей, которые он собирает
   (`build_loop`). Если ввести абстракцию `Executor`, нативный `AgentLoop`
   становится её первой реализацией, а внешний агент — второй. Ядро выше и
   ниже шва не трогается.

2. **Периметр надёжнее кооперации.** Хуки/permission-callbacks у всех агентов
   разные (у Claude Code — hooks, у Codex — approval policy, у OpenCode —
   plugins) и все — кооперативные: полагаются на то, что агент их честно
   вызывает. Единственная гарантия, не зависящая от честности агента, —
   запуск **всего процесса агента внутри sandbox Svarog** (docker, non-root,
   лимиты, mounts по allowlist). Поэтому базовый уровень enforcement —
   containment, а хуки — опциональное усиление.

3. **LLM-прокси превращает ослабление сети обратно в enforcement-точку.**
   Наивный вариант «egress-allowlist по доменам» слаб (SNI/DNS-обходы,
   ключ подписки в env sandbox'а). Вместо этого агент направляется на
   **Svarog-owned LLM-прокси** (все три агента умеют custom base_url:
   `ANTHROPIC_BASE_URL` / `model_providers.base_url` / providers-конфиг).
   Сеть sandbox'а остаётся почти-«off»: internal-only network без default
   route и DNS наружу, единственный достижимый хост — прокси. Прокси даёт
   сразу три свойства уровня enforcement, а не кооперации:
   * **ключ провайдера инжектируется на прокси (host-side)** и никогда не
     попадает в sandbox — exfiltration ключа невозможна по построению;
   * **учёт токенов/стоимости считается на прокси** — бюджеты и per-tenant
     квоты (ADR-0012/0014, HTTP 429) перестают зависеть от честности
     stream-событий агента;
   * egress с прокси прибит к одному endpoint провайдера.

4. **MCP — общий контракт для «обратных» инструментов.** Все три агента
   умеют быть MCP-клиентами. Svarog может отдать им свои инструменты
   (`remember`, `read_memory`, `read_skill`, `create_skill_proposal`,
   `request_approval`, `ask_user`) как **MCP-сервер**: память и governance
   остаются под single-writer'ом и policy Svarog, без парсинга чужих
   форматов. Модуль `mcp/` уже есть (клиентская сторона), добавляется
   серверная.

5. **Suspend-resume — общий язык двух миров.** Run Svarog — resumable state
   machine (ADR-0005); сессии всех трёх агентов тоже resumable
   (`claude --resume`, `codex exec resume`, session id у OpenCode). Значит,
   всё долгоживущее ожидание (approval, ask_user, бюджет-стоп) отображается
   не в «держать контейнер живым часами», а в родной для Svarog паттерн:
   checkpoint session id → остановить контейнер → run в
   `waiting_approval`/`suspended` → на решении поднять сессию заново.

## Решение

### 1. Абстракция Executor

```python
class Executor(Protocol):
    async def run(self, task: str, ctx: RunContext) -> RunOutcome: ...
    async def resume(self, run: Run, ctx: RunContext) -> RunOutcome: ...
```

* `NativeExecutor` — текущий `AgentLoop` (поведение по умолчанию, без
  изменений).
* `ExternalAgentExecutor` — драйвер headless-режима внешнего агента через
  **адаптер**:

```python
class AgentAdapter(Protocol):
    def command(self, task: str, session: str | None) -> list[str]:
        """CLI-команда headless-запуска (print/exec-режим, JSON-stream)."""
    def parse_event(self, line: str) -> AgentEvent | None:
        """Нормализация stream-события агента в общее AgentEvent."""
    def session_id(self, events: list[AgentEvent]) -> str | None:
        """Идентификатор сессии агента для resume."""
    def context_files(self, ctx: RunContext) -> dict[str, str]:
        """Файлы контекста в workspace (CLAUDE.md / AGENTS.md / …)."""
    def base_url_env(self, proxy_url: str) -> dict[str, str]:
        """Env, направляющий агента на LLM-прокси Svarog."""
    def state_dir(self) -> PurePosixPath:
        """Каталог состояния агента в контейнере (~/.claude, ~/.codex, …) —
        монтируется из persistent agent-state volume (§5)."""
    def managed_policy(self, snapshot: PolicySnapshot) -> tuple[PurePosixPath, str] | None:
        """(путь, содержимое) read-only managed-настроек с высшим
        приоритетом (hooks/permissions), если агент их поддерживает (§6)."""
    def capabilities(self) -> AdapterCapabilities:
        """hooks / resume / mcp / managed_policy — матрица возможностей."""
```

Адаптеры MVP: `claude-code` (`claude -p --output-format stream-json`),
`codex` (`codex exec --json`), `opencode` (`opencode run --format json`).
Общее у всех троих: headless-запуск, JSONL-стрим событий, resume по
session id, custom base_url, MCP-клиент — это и есть минимальный контракт.

### 2. Инвариант изоляции: агент живёт в sandbox целиком

Процесс внешнего агента запускается **внутри** `ExecutionEnvironment`
(тот же слой, что для bash-tool): non-root, лимиты CPU/RAM, wall-clock
timeout, workspace как единственный writable-mount, control-plane
(`.svarog/`, memory-repo, skills-repo, tenants/) не примонтирован вообще.
Всё, что агент делает своими встроенными инструментами (файлы, bash,
свой git), заперто в workspace физически, а не по договорённости.

**Сеть.** Вместо `--network none` — **internal-only network**: приватная
docker-сеть без default route и без DNS наружу; в ней ровно два
Svarog-owned соседа — LLM-прокси (§3) и bridge-socket недоступен по сети
(это mount, см. §4). С точки зрения инварианта ADR-0002 это не
«allowlist доменов», а «сеть off, кроме одного внутреннего hop к
компоненту Svarog». Конфигурация сети входит в `config_digest`
(§0.4 ADR-0015) — drift ловится на resume. Без docker external-executor
**fail-closed**: режим `local-trusted` для чужого агента не поддерживается
(для standard-тенантов это уже так по ADR-0013).

**`.git` внутри workspace.** Внешний агент легально пишет в `.git`
(коммитит сам) — в отличие от нативных tools, denylist на `.git` к нему
неприменим. Это безопасно за счёт уже реализованного хост-слоя
(ADR-0015 §0.2): всякий host-git (Flow C: pull/commit/push) исполняется с
`core.hooksPath=/dev/null` и отрезанными global/system-конфигами, поэтому
подложенные агентом hooks/конфиги на хосте инертны; для инициализируемых
харнессом деревьев дополнительно действует `separate_git_dir`. Push
по-прежнему невозможен изнутри (нет сети и кредов) — публикация только
через `svarog push` с policy и secret scan (Flow C не меняется).

### 3. LLM-прокси: секреты, бюджеты, квоты

Прокси — маленький reverse-proxy (расширение gateway или sidecar-процесс),
слушает в internal-сети sandbox'а и форвардит на endpoint провайдера.

* **API-key-режим (основной):** ключ (`executor.external.api_key_ref` →
  SecretStore, host-скоуп `TaskRunner.host_store`) подставляется в
  заголовок **на прокси**. В sandbox ключа нет ни в env, ни в файлах —
  redaction для него не нужна, утечка невозможна по построению.
* **Subscription/OAuth-режим (opt-in, слабее):** подписочные креды
  (`claude login` и т.п.) живут в agent-state volume (§5) внутри sandbox;
  в allowlist прокси добавляются auth-endpoints провайдера. Честный
  trade-off: креды доступны коду в sandbox, но они и так принадлежат
  этому агенту и скоупятся провайдером; фиксируется в доке оператора.
* **Метеринг = enforcement:** прокси считает токены/стоимость по телам
  ответов провайдера. Это единственный источник истины для бюджетов
  (usage-события из стрима агента — только для UX-прогресса). Превышение
  бюджета run'а или квоты тенанта → прокси отвечает 429, executor
  переводит run в `suspended` (родной путь ADR-0005: поднять лимит →
  `svarog resume`).

Остаточный риск, который прокси закрыть не может принципиально:
exfiltration данных workspace **в тело запроса к LLM** — любой агент с
доступом к модели может закодировать данные в prompt. Сужение: адресат
такой утечки — только аккаунт провайдера оператора, не произвольный хост.

### 4. Bridge-socket: один канал для всей кооперации

В sandbox монтируется ровно один unix-socket
(`/run/svarog/bridge.sock`, read-only mount файла), поверх него — два
протокола:

* **MCP-сервер Svarog** (через shim в образе: stdio ↔ socket):
  `remember` / `read_memory` (single-writer и прогрессивная загрузка
  ADR-0004/0011 как есть), `read_skill` / `create_skill_proposal`
  (Flow B governance как есть), `request_approval` / `ask_user`.
* **Hook-endpoint** для tier 2 (§6): `PreToolUse` → policy-решение.

Bridge — осознанная поверхность атаки: до него дотягивается и bash агента.
Принцип безопасности: **bridge не даёт ничего сверх того, что дали бы
нативные tools Svarog** — за каждым вызовом стоят те же PolicyEngine,
memory-очередь с provenance (run_id) и governance-review; плюс
rate-limit на вызовы. Спам в память не страшнее, чем от нативного агента:
он виден в очереди/trace и ревьюится.

Контекст на входе: `context_builder` рендерит то же, что сейчас идёт в
system prompt (индекс памяти + профиль + карточки скиллов), в файлы
контекста агента (`adapter.context_files` → `CLAUDE.md` / `AGENTS.md`)
при подготовке workspace. Чтение — файлами, запись — только через MCP.

### 5. Agent-state volume: resumability переживает контейнер

Состояние сессии агента (`~/.claude`, `~/.codex`, …) не может жить в
эфемерном контейнере — иначе suspend/resume и recovery теряют сессию.
Каждому тенанту выделяется **persistent agent-state volume**
(`tenants/<id>/agent-state/<adapter>/`), монтируемый в
`adapter.state_dir()`. Свойства:

* лежит в tenant home → изоляция арендаторов сохраняется автоматически
  (ADR-0012), ни один volume не разделяется между тенантами;
* содержит содержимое диалогов и, в subscription-режиме, креды → входит в
  denylist/gitignore (ADR-0006) и никогда не попадает в git-flows;
* `Run.meta.agent_session_id` + этот volume = полный материал для resume.

### 6. Два уровня enforcement

* **Tier 1 — containment (обязательный, для любого агента).** Гарантии
  только от периметра: workspace-mount, no control-plane, internal-only
  network + LLM-прокси, секреты — только явно инжектированные
  (`secrets.inject`; ключ провайдера — вообще не в sandbox, §3), redaction
  стрима по `known_secret_values()`. Critical-действия (push, деплой,
  выдача секретов) агенту **физически недоступны** — публикация только
  host-flow.
* **Tier 2 — cooperative (опциональный, где адаптер умеет).** Policy Engine
  подключается к нативному permission-механизму агента через
  hook-endpoint bridge'а: `allow / deny / require_approval` из
  замороженного на старте run снапшота (ADR-0010).

  **Защита от самоослабления:** hooks/permissions передаются агенту НЕ
  через workspace-файлы (`.claude/settings.json` агент может переписать
  сам — он легально пишет в workspace), а через
  `adapter.managed_policy()` — **read-only mount managed-настроек по
  пути с высшим приоритетом** (у Claude Code — managed settings,
  переопределяющие project-слой). Файл генерируется при старте run из
  замороженного policy-снапшота; изменить его изнутри sandbox нельзя.
  Это не делает tier 2 некооперативным (бинарь агента всё ещё должен
  честно применять настройки), но убирает самый дешёвый обход —
  переписывание собственного конфига из workspace.

  **Tier 2 никогда не заменяет tier 1** — это UX-слой (supervised-режим,
  notify, точечные deny), а не граница безопасности.

Режимы автономии: `yolo` работает на tier 1; `--supervised` требует
tier 2 — адаптер без hook-моста (`capabilities().hooks == False`) обязан
отклонить supervised-запуск fail-closed, а не молча деградировать.

### 7. Approval и ask_user: grace period → suspend-resume

Ожидание человека может длиться часы и дни — держать контейнер живым
нельзя (timeout sandbox'а, ресурсы, хрупкость). Схема едина для
`require_approval` (hook, tier 2) и `request_approval`/`ask_user` (MCP):

1. Вызов блокируется на **grace period** (конфигурируемый, по умолчанию
   ~120 с) — интерактивный supervised-сценарий получает решение сразу,
   без цикла suspend.
2. По истечении grace: hook/MCP отвечает
   `deny: pending approval SVAROG-<id>`; executor фиксирует
   `agent_session_id`, **останавливает контейнер**, run →
   `waiting_approval`. Решение приходит через любой канал
   (CLI/gateway/Telegram) — как сейчас.
3. **Decision cache:** решение сохраняется в БД с ключом-отпечатком
   вызова (tool + нормализованный hash аргументов, скоуп run'а).
4. На approve executor поднимает сессию (`adapter.command(session=…)`)
   с инжектированным сообщением «approval <id> granted — повторите
   действие»; агент ретраит вызов, hook сверяется с decision cache и
   пропускает. На deny — резюм с причиной отказа; агент продолжает с этим
   знанием (симметрично нативному поведению). Ответ `ask_user`
   доставляется тем же resume-сообщением.

### 8. Trace и жизненный цикл

* Адаптер нормализует JSONL-стрим в `AgentEvent`
  (text / tool_call / tool_result / usage / result), executor пишет их тем
  же `TraceRecorder` — trace един для нативного и внешнего исполнения;
  `Run.meta` дополняется `executor`, `adapter`, `agent_session_id`.
  Redaction — до записи каждого события.
* **Флуд-контроль:** крупные tool_result-события идут через уже
  существующую персистенцию больших tool-результатов (ADR-0015 §1.2) —
  в trace ссылка + превью, не мегабайты. **Неизвестные типы событий**
  сохраняются как opaque-записи с raw JSON — forward-compat при дрейфе
  формата стрима.
* **Lease/recovery:** executor бьёт heartbeat (§0.5 ADR-0015) на каждом
  событии стрима; cancel/suspend → `docker stop`;
  `recover_interrupted_runs` по протухшему heartbeat находит упавший run,
  а agent-state volume (§5) даёт материал для resume.
* **Дрейф CLI-контрактов:** версия агента пинится в образе sandbox;
  на каждый адаптер — golden-JSONL contract-тесты (записанные фикстуры
  стрима, без сети); `svarog doctor` проверяет, что версия агента в
  образе входит в поддерживаемый адаптером диапазон.
* Verifier (§6.11) не меняется: после завершения — тесты/линтеры/secret
  scan рабочего дерева, приоритет над самооценкой агента; exit code 4.
* Refuel Svarog для external-executor выключен (у агентов своя
  компакция); лимит стоимости/токенов enforc'ится прокси (§3),
  wall-clock — sandbox'ом.

### 9. Субагентная делегация: внешний агент как инструмент нативного цикла

Executor-swap (§1) отдаёт внешнему агенту run целиком — Svarog остаётся
периметром. Второй, «более harness», режим — **делегация**: run ведёт
нативный `AgentLoop` (скиллы, память, per-tool policy, supervised — всё
работает как обычно), а внешний агент вызывается для очерченной подзадачи
через уже существующий `spawn_child_run` (ADR-0015 фаза 3) с
`executor: "external"`:

* ребёнок — обычный child run в изолированном git-worktree с клампнутыми
  бюджетами, но его data-plane — `ExternalAgentExecutor`; вся инфраструктура
  фазы 1 (containment, стрим → trace, redaction) переиспользуется как есть;
* сама делегация — policy-проверяемый tool call (`run.spawn_child`):
  правила могут требовать approval или запрещать её; в trace видно,
  почему и что делегировано;
* секция `executor.external` при `executor.type: native` — конфигурация
  «делегация доступна»; без неё делегация возвращается модели tool-ошибкой
  (родитель делает подзадачу нативно), как и fail-closed отказ без docker;
* результат ребёнка возвращается родителю tool-результатом; работа —
  на ветке ребёнка через host-flow commit с secret scan (Flow C как есть);
* глубина дерева — один уровень (как у нативных child runs): внешнему
  ребёнку никакие tools Svarog не выдаются вовсе.

Оговорка: внутри worktree `.git` — файл-указатель на git-dir родителя вне
bind-mount, поэтому собственный git внешнего агента в worktree не работает
(и не должен: коммит — host-flow). Trade-off'ы качества (двойной reasoning,
lossy-передача контекста подзадачи промптом) — осознанные свойства режима;
выбор executor-swap vs делегация — per-task решение модели/оператора.

### 10. Конфигурация

```yaml
executor:
  type: native            # native | external; default native
  external:
    adapter: claude-code   # claude-code | codex | opencode
    image: svarog/agent-claude:1.2.3    # версия агента пинится тегом
    auth: api-key                       # api-key | subscription
    api_key_ref: CLAUDE_CODE_KEY        # секрет → инжекция НА ПРОКСИ (§3)
    enforcement: cooperative            # containment | cooperative
    approval_grace_seconds: 120         # §7
```

Мультиарендность (ADR-0012/0014) работает без изменений: executor-конфиг —
часть per-tenant cfg, standard-роль уже заклампана в docker; agent-state
volume и метеринг прокси — per-tenant.

## Сводка тонких проблем → механизм

| Проблема | Механизм |
|---|---|
| Ключ провайдера в sandbox → exfiltration | инжекция на LLM-прокси; ключ не входит в sandbox (§3) |
| Egress-allowlist по доменам обходим (SNI/DNS) | internal-only network, единственный hop — прокси (§2) |
| Бюджеты по stream-событиям = доверие агенту | метеринг на прокси = enforcement; 429 → suspend (§3) |
| Approval на часы держит контейнер | grace period → suspend-resume + decision cache (§7) |
| Сессия агента гибнет с контейнером | per-tenant agent-state volume (§5) |
| Агент переписывает свои hooks из workspace | managed-policy read-only mount с высшим приоритетом (§6) |
| Hook-planting в `.git` workspace | уже закрыто host-git hardening'ом ADR-0015 §0.2 (§2) |
| Bridge-socket как поверхность атаки | даёт не больше нативных tools; policy + provenance + rate-limit (§4) |
| Мегабайтные tool_result во trace | персистенция больших результатов ADR-0015 §1.2 (§8) |
| Дрейф stream-формата/флагов CLI | пин версии в образе + golden-JSONL тесты + doctor-gate (§8) |
| Supervised без hook-поддержки | fail-closed отказ по capabilities (§6) |
| Exfiltration в тело LLM-запроса | принципиально не закрывается; сужен до аккаунта провайдера (§3) |

## Фазы

* **Фаза 1 — containment MVP.** `Executor`-абстракция, `NativeExecutor`
  (рефактор без изменения поведения), адаптер `claude-code`,
  internal-network + LLM-прокси (api-key-режим, метеринг), agent-state
  volume, маппинг stream-json → trace (включая §1.2-персистенцию и
  opaque-события), verifier, redaction, golden-JSONL тесты. Только yolo.
* **Фаза 2 — bridge-socket + MCP-сервер.** `remember`/`read_memory`/
  `read_skill`/`create_skill_proposal`/`ask_user` (с suspend-resume §7);
  рендер контекста в `CLAUDE.md`/`AGENTS.md`.
* **Фаза 3 — cooperative tier.** Hook-мост policy/approval через bridge,
  managed-policy mount, decision cache, supervised-режим; `chat` поверх
  agent-сессий; subscription-режим прокси.
* **Фаза 3.5 — субагентная делегация (§9).** `spawn_child_run` с
  `executor: "external"`: нативный цикл — оркестратор, внешний агент —
  инструмент для подзадач в worktree. Дельта маленькая — строится целиком
  на фазе 1 и child runs ADR-0015.
* **Фаза 4 — второй и третий адаптеры.** `codex`, `opencode` — проверка
  агент-агностичности контракта; публикация матрицы capabilities
  (hooks / resume / mcp / managed_policy) в доке.

## Обратная совместимость

`executor.type: native` — default; ни один существующий сценарий не
меняется. External-executor — opt-in, требует docker (fail-closed).
Reference-analysis не ревизится: model-agnostic API-подход остаётся
основным; это дополнительный режим исполнения, а не замена нативного loop.

## Последствия

* **Плюсы:** зрелый data-plane (reasoning, встроенные coding-tools,
  компакция) бесплатно; подписочная экономика вместо per-token API; backbone
  Svarog (память, governance, trace, tenancy, verifier) сохраняется;
  бюджеты/квоты и секрет провайдера получают **более сильные** гарантии,
  чем «доверять стриму» (прокси-enforcement).
* **Минусы и trade-off'ы (честно):**
  * Гранулярность контроля ниже нативной: внутри workspace агент действует
    без per-tool policy (tier 1); tier 2 — кооперативный, обходим
    злонамеренной моделью даже с managed-policy (бинарь должен честно её
    применять). Граница безопасности — только периметр.
  * Новые компоненты в эксплуатации: LLM-прокси и bridge-shim; прокси —
    на критическом пути каждого LLM-вызова (доступность, латентность).
  * Subscription-режим слабее api-key-режима (креды в sandbox) — выбор
    оператора, зафиксирован в конфиге и `config_digest`.
  * Сопровождение адаптеров под дрейф CLI-контрактов — постоянная
    стоимость; смягчена пином версий и golden-тестами, но не устранена.
  * ToS подписок: режим «подписка агента как backend» может ограничиваться
    провайдером — ответственность на операторе, фиксируется в доке.
  * Trace беднее нативного: видим то, что агент стримит, а не всё, что он
    делает (bash внутри агента виден как событие, но без посредничества
    нашего ToolRegistry).

## Альтернативы

1. **Только LLM-provider bridge** (Anthropic Messages API как ещё один
   `ModelProvider`). Проще и уже совместимо с архитектурой, но не даёт
   ценности зрелого агента (его tools, компакция, подписка). Не конкурент,
   а ортогональная фича — может быть сделана независимо.
2. **Встраивание через Agent SDK** (in-process, свои tool-реализации).
   Глубже контроль для Claude, но не агент-агностично (Codex/OpenCode не
   покрыты), тянет чужой рантайм в процесс Svarog и всё равно кооперативно.
   Отклонено в пользу процессной границы + адаптеров.
3. **Перехват на уровне LLM-прокси c policy по телам запросов** (MITM,
   разбор tool-calls из трафика). Наш прокси намеренно НЕ разбирает
   семантику — только auth, метеринг, форвардинг: разбор тел — это снова
   «распознавание», а не enforcement (ADR-0002), и хрупко к смене формата.
   Отклонено как механизм контроля; принято как транспорт/метеринг.
4. **Egress-allowlist по доменам без прокси.** Обходим (свой DNS, SNI),
   ключ остаётся в sandbox. Отклонено в пользу internal-network + прокси.
5. **Ничего не делать** (только нативный loop). Сохраняет чистоту, но
   оставляет запрос «плюсы Svarog поверх зрелого агента» без ответа;
   пользователи соберут такой мост сами — без sandbox и policy. Отклонено.
