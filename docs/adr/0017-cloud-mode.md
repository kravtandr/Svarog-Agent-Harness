# ADR-0017: Cloud-режим — постоянный мультитенантный сервер поверх svarog serve

## Статус

Принято (Фаза 1 реализована)

## Контекст

Svarog работает в двух режимах на одном core (§10, README «Режимы работы»):
локальный CLI (готов) и **cloud-агент** — постоянно работающий инстанс на
сервере, к которому клиенты подключаются удалённо. Технический фундамент
второго режима уже есть и приниматься заново не должен:

* `svarog serve` — REST/WS gateway (§10.4): create/list/get run, WS-стрим
  событий, approvals/ask_user, refuel-супервизор в lifespan;
* мультиарендность ADR-0012/0013/0014 (Фазы 1–3 реализованы): tenant =
  agent-home, роли superuser/standard с клампом и fail-closed docker,
  per-tenant bearer/JWT auth, квоты → 429, `svarog tenant …`, first-touch;
* Flow C (`gitflow/workspace.py`): pull → task-ветка → guarded commit → push
  host-компонентом; hardened-git (ADR-0015 §0.2).

Чего нет — и что фиксирует этот ADR — это **продуктовый слой** поверх
фундамента: (1) откуда в серверном workspace берётся код пользователя,
(2) чем пользователь подключается к серверу, (3) как тенанты управляются
без shell-доступа к хосту, (4) как это разворачивается и не дырявит
периметр. Скоуп: **self-hosted сервер команды/компании** (провижн тенантов
админом); публичный SaaS — вне скоупа, но развилки выбраны так, чтобы его
не отрезать.

## Принятые развилки

1. **Cloud-режим — это deployment-профиль `svarog serve`, а не новая
   подсистема.** Ни второго бинаря, ни форка ядра: тот же gateway + tenancy,
   дополненные недостающими операциями. Всё, что ниже `GatewayService`,
   не меняется (продолжение линии ADR-0012 «изоляция резолвингом, не ядром»).
2. **Два равноправных источника workspace: git-клон и постоянный серверный
   workspace.** Первый путь: клиент передаёт в `create_run` спецификацию
   репозитория (`repo: {url, ref}`), сервер host-side клонирует его в
   одноразовый task-workspace и дальше работает штатный Flow C (task-ветка →
   commit → push). Второй путь: **named workspace** — постоянный каталог
   тенанта на сервере (`workspace: <name>`), который живёт между runs и
   сессиями: агент накапливает в нём результаты, а пользователь забирает их
   через API/CLI. Named workspace может и не быть git-репозиторием (агент
   волен сделать `git init` сам — Flow C это уже умеет через
   `WorkspacePrep(is_git=False)`), и не подметается retention-GC. Без обоих
   полей — одноразовый пустой каталог, как сейчас. Sync/upload рабочей папки
   с клиента — отклонено для MVP (конфликты, размер, секреты в рабочей
   копии); может добавиться позже как отдельный транспорт, не меняя модель.
3. **Git-credentials — per-tenant секрет, резолвится только host-side.**
   Токен/deploy-key лежит в tenant SecretStore под конвенциональным именем
   (`git.credentials`, override — `repo.credentials_ref`); используется
   только процессом git на хосте (clone/pull/push и так host-flow по
   ADR-0002/0003) и никогда не попадает в sandbox, контекст модели или
   trace (redaction по known_values). Глобального git-ключа нет — у каждого
   тенанта свой доступ, чтобы тенант A не мог клонировать приватный репо B.
4. **Клиент №1 — тот же `svarog` CLI в режиме remote; клиент №2 — сырой
   REST/WS API.** `remote`-профиль в user-конфиге (`~/.svarog/svarog.yaml`)
   переключает команды `run/chat/resume/traces/approvals/skills` с локального
   исполнения на вызовы gateway. Web UI — отдельный roadmap-пункт (§10.3),
   Telegram уже работает поверх hub и в изменениях не нуждается.
5. **Admin-plane отделён от tenant-plane.** Управление тенантами по HTTP —
   отдельные `/admin/*`-роуты под отдельным admin-токеном; tenant-токен на
   них не работает (и наоборот admin-токен не является тенантом). До
   появления admin-plane управление — только `svarog tenant …` на хосте.
6. **TLS не встраиваем — терминируется reverse-proxy.** Свой TLS-стек в
   uvicorn не тянем; вместо этого закрываем текущую щель: в multi-tenant
   режиме сетевой bind (не loopback) требует явного `--behind-proxy`
   (декларация «передо мной TLS-прокси»), иначе отказ — bearer-токены
   поверх открытого HTTP недопустимы.

## Решение: швы интеграции

### 1. Провижн workspace (сервер-сайд): git-клон или named workspace

`CreateRunRequest` расширяется опциональной спецификацией источника
workspace (поля взаимоисключающие):

```python
class RepoSpec(BaseModel):
    url: str                       # https:// или ssh:// / scp-like
    ref: str | None = None         # ветка/тег; None — default branch
    credentials_ref: str | None = None  # имя секрета в tenant-store; None — "git.credentials"

class CreateRunRequest(BaseModel):
    task: str
    autonomy: AutonomyMode | None = None
    repo: RepoSpec | None = None       # клонировать в одноразовый task-workspace
    workspace: str | None = None       # имя постоянного named workspace тенанта
    # оба None — одноразовый пустой каталог; оба заданы — 422
```

Новый host-side компонент `gitflow/provision.py` (`provision_workspace`).

**Ветка named workspace** (без git-механики):

1. имя валидируется слагом (`[a-z0-9-]{1,64}`) — не путь, `..`/`/`
   отклоняются до резолвинга;
2. каталог `home/workspaces/named/<name>`; должен быть создан заранее
   (`POST /workspaces`) — run по несуществующему имени отвечает 404, а не
   создаёт молча (защита от опечаток, размазывающих результаты по каталогам);
3. run исполняется прямо в этом каталоге; параллельный run в том же
   workspace — 409 (busy-лок в реестре workspace'ов: два агента в одном
   дереве без git-веток конфликтуют напрямую);
4. если каталог — git-репо (агент сделал `git init` или админ положил),
   `WorkspaceFlow.start` работает штатно; если нет — как обычный не-git
   workspace (`is_git=False`), это валидный режим.

**Ветка git-клона**:

1. каталог `home/workspaces/tasks/<run-slug>` (строго под home тенанта —
   тот же confinement, что в ADR-0014);
2. `git clone` hardened-флагами `GitRepo` + дополнительно
   `protocol.file.allow=never` (запрет `file://` — кросс-тенантное чтение
   хоста через clone) и допуск только `https/ssh`-схем;
3. credentials: для https — одноразовый `GIT_ASKPASS`-скрипт, читающий
   секрет из tenant-store; для ssh — `GIT_SSH_COMMAND` с per-tenant key-файлом
   0600 внутри home тенанта. В env агента/sandbox не попадает ничего;
4. дальше без изменений: `WorkspaceFlow.start` (pull уже сделан клоном,
   task-ветка), push по autonomy/policy (protected ветки — approval, как есть).

Опциональный per-tenant `repo_allowlist` (glob по URL) в реестре: standard-
тенанту можно ограничить, откуда он клонирует. Пустой список = без ограничений
(team-сценарий), но точка для SaaS уже есть.

Судьба workspace после run: task-workspace живёт для `resume` и инспекции,
GC — по `cloud.workspace_retention_days` для терминальных run'ов
(переиспользует механику подметания из ADR-0016 GC). **Named workspace
retention-GC не трогает никогда** — он удаляется только явным
`DELETE /workspaces/{name}` (или админом на диске); это и есть его
назначение — накапливать состояние между задачами.

### 2. Доукомплектование API до «управляемо снаружи»

Недостающие операции (все — tenant-scoped, через существующий
`_require_service`):

* `POST /runs/{id}/resume` — явное возобновление suspended-run
  (`service.resume_run` уже есть, наружу не выведен);
* `POST /runs/{id}/cancel` — cooperative-cancel: пометка в БД +
  отмена фоновой asyncio-задачи между итерациями; run уходит в `cancelled`
  (новая работа в `TaskRunner`, вежливая — checkpoint сохраняется);
* `GET /runs/{id}/diff` — `git diff <base>...<task-branch>` workspace'а
  run'а (host-side, read-only) — главный «артефакт» для ревью с клиента;
* `GET /whoami` — tenant_id, role, квоты/usage (клиенту для UX, админу
  для отладки auth).

Lifecycle и содержимое named workspaces (всё — в границах
`home/workspaces/named/` тенанта, пути через тот же confinement, что
file_tools):

* `POST /workspaces {name}` — создать; `GET /workspaces` — список
  (имя, размер, занят ли run'ом, mtime);
* `GET /workspaces/{name}/files?path=` — листинг каталога /
  скачивание файла; `GET /workspaces/{name}/archive` — tar.gz снапшот
  (способ забрать результаты не-git workspace'а);
* `DELETE /workspaces/{name}` — удалить (отказ 409, если в нём активный run);
* результаты git-workspace'ов по-прежнему забираются «правильным» путём —
  push task-ветки, `files/archive` — это транспорт для рабочих файлов и
  не-git состояния, а не замена Flow C.

Сессии для remote-chat (семантика §10.1 «сообщение = run, session агрегирует
runs и разделяет workspace»):

* `POST /sessions {repo? | workspace?}` → `session_id`; с `repo` — провижн
  клона один раз на сессию, с `workspace` — сессия работает в named
  workspace (типовой сценарий «продолжаем вчерашнее»);
* `POST /sessions/{id}/messages {text}` → run в workspace сессии;
* `GET /sessions/{id}` → runs сессии.

### 3. Thin CLI: remote-профиль

В user-слой конфига добавляется секция:

```yaml
# ~/.svarog/svarog.yaml
remote:
  url: https://svarog.team.example
  token_ref: svarog_remote_token   # bearer/JWT в локальном SecretStore клиента
```

* `svarog --remote run "..." --repo git@…` или `--workspace <name>` →
  `POST /runs` + WS-attach (стрим текста/tool_call/notify — уже публикуется
  `GatewayService`);
* `svarog --remote chat [--workspace <name>]` → сессии из §2; approvals
  решаются прямо из чата;
* `svarog --remote workspace create|list|pull|rm` — lifecycle named
  workspaces (`pull <name> [path]` — скачать файл/архив результатов);
* `--remote` также у `resume/traces/approvals/skills` — тонкий маппинг
  1:1 на REST, без локального состояния (никаких локальных БД/памяти);
* `svarog login <url>` — интерактивно сохранить URL+токен в профиль.

Инвариант UX: в remote-режиме локально не исполняется **ничего** — нет
«наполовину локального» run. Флаг и профиль только переключают транспорт.

### 4. Admin-plane (управление тенантами без shell)

Роутер `/admin/*` в том же приложении, auth — отдельный
`admin_token_ref` (глобальный SecretStore, как `gateway.token_ref`):

* `POST /admin/tenants {id, role, quotas}` → переиспользует
  `tenant/provision.py` (то же, что `svarog tenant create`);
* `GET /admin/tenants`, `GET /admin/tenants/{id}` (+usage из квот);
* `POST /admin/tenants/{id}/token` — выпуск/ротация bearer;
* `DELETE /admin/tenants/{id}` — деактивация (реестр помечает, home
  не удаляется — данные тенанта стираются только руками админа).

Tenant-резолвер admin-токен не принимает; admin-резолвер не отдаёт
`GatewayService` (нет «супер-тенанта», который видит чужие runs) — просмотр
чужих трасс намеренно не входит в admin-plane MVP (это отдельное решение о
приватности, его не принимаем мимоходом).

### 5. Deployment-профиль

* **Рекомендуемая топология**: `svarog serve` на хосте (systemd unit) +
  docker для sandbox'ов standard-тенантов + reverse-proxy (Caddy/nginx) с TLS.
  Сервер в контейнере — возможен, но требует docker-socket внутрь
  (docker-out-of-docker) — задокументированный трейд-офф, не дефолт;
* `docker compose` файл (roadmap §24): svarog-serve + caddy (+ опц. локальная
  модель) — smoke-профиль для «поднять одной командой»;
* Гарда bind'а: в multi-tenant режиме не-loopback `--host` без
  `--behind-proxy` → отказ (сейчас проверка есть только в single-tenant —
  щель закрывается);
* Rate-limit на auth-fail (после N неудачных попыток токена с IP — пауза),
  auth-fail в лог; полноценный /metrics — post-MVP;
* Бэкап = снапшот agent-home (файлы + git-репо + SQLite): задокументировать
  `sqlite3 .backup`-friendly остановку или WAL-copy, ничего нового в коде.

## Конфигурация

```yaml
cloud:                       # активен только вместе с tenancy.enabled
  workspace_retention_days: 14   # GC терминальных task-workspace'ов; 0 — не чистить
                                 # (named workspaces GC не подлежат)
  max_named_workspaces: 20       # на тенанта; 0 — named workspaces выключены
gateway:
  admin_token_ref: null      # секрет admin-plane; null — /admin/* выключены
# per-tenant (реестр): repo_allowlist: ["https://git.team.example/*"]
# клиент (~/.svarog/svarog.yaml): секция remote (см. §3)
```

## Инварианты (тесты — часть Definition of Done)

* **Git-credentials confinement**: секрет резолвится из tenant-store
  host-side; не появляется в env sandbox, в контексте модели и в trace
  (redaction); тенант A с собственным `git.credentials` не может
  клонировать через секрет B.
* **Clone confinement**: целевой путь строго под `tenants/<id>/workspaces/`;
  `file://`, `ext::` и прочие не-https/ssh схемы отклоняются до запуска git.
* **Named-workspace confinement**: имя — слаг, не путь (`..`, `/`, symlink →
  отказ до резолвинга); `files/archive`-роуты не выходят за
  `workspaces/named/<name>` (тот же confinement-валидатор, что у file_tools);
  workspace тенанта A недоступен по имени из тенанта B.
* **Busy-лок**: второй одновременный run в одном named workspace → 409;
  `DELETE` занятого workspace → 409.
* **Plane separation**: tenant-токен на `/admin/*` → 401/403; admin-токен
  на tenant-роуты → 401/403.
* **Bind guard**: multi-tenant + не-loopback bind без `--behind-proxy` →
  отказ на старте.
* **Remote purity**: команды с `--remote` не открывают локальную БД, не
  пишут в локальную память и не запускают локальный `TaskRunner`.
* **Cancel safety**: cancel между итерациями сохраняет checkpoint;
  `resume` отменённого run'а — явная ошибка, не тихий рестарт.
* **Quota на провижн**: clone учитывается в active-runs квоте (провижн —
  часть create_run), 429 до, а не после клона.

## Фазы

* **Фаза 1 — серверные workspaces. ✅ выполнено.** Оба источника: `RepoSpec`
  в API + `gitflow/provision.py` (клон + GIT_ASKPASS-credentials +
  confinement + `_PROTOCOL_FLAGS`-эшелон) и named workspaces
  (`POST/GET/DELETE /workspaces`, busy через workspace-lease ADR-0015 §0.5,
  `files/archive`), `GET /runs/{id}/diff` (Run-Id trailer + uncommitted),
  `POST /runs/{id}/resume`, retention-GC в supervise-цикле. Runner-per-run
  workspace в `GatewayService._runner_for`; resume в своём workspace
  переиспользует entry-конфиг (`TaskRunner._runner_for_resume`) — конфиг не
  перечитывается из склонированного репо. Тесты:
  `tests/test_cloud_workspaces.py`.
* **Фаза 2 — thin CLI.** `remote`-профиль + `svarog login`, маппинг
  `run/resume/traces/approvals/skills/workspace`, WS-attach, сессии +
  remote-chat, `POST /runs/{id}/cancel`, `GET /whoami`.
* **Фаза 3 — admin-plane и упаковка.** `/admin/*` (create/list/token-rotate/
  deactivate), bind-guard `--behind-proxy`, rate-limit auth, systemd unit +
  docker compose + reverse-proxy гайд, retention/бэкап-доки.
* **Пост-MVP (вне ADR):** Web UI (§10.3), shared-Postgres (`tenant_id`,
  ADR-0014), k8s/microVM-runner, upload-транспорт workspace, SaaS-слой
  (self-signup, billing) — точки расширения: first-touch provisioning,
  `repo_allowlist`, квоты.

## Последствия

* Cloud-режим не порождает второго кода-пути: локальный CLI остаётся частным
  случаем (single-tenant, без remote-профиля), ядро и tenancy не меняются;
  почти вся новизна — на границе (API-модели, провижн workspace, admin-роуты).
* Сервер получает право сетевого git-clone по заданию клиента — это новый
  outbound-канал хоста. Он ограничен: схемы https/ssh, credentials
  per-tenant, опц. allowlist; но SSRF-поверхность (клон с внутренних хостов)
  в team-сценарии принимается, для SaaS обязателен allowlist.
* Диск становится ресурсом квотирования де-факто (клоны репозиториев +
  named workspaces, которые не подметаются GC по построению); MVP сдерживает
  это retention-GC task-workspace'ов и лимитом `max_named_workspaces`,
  дисковые квоты по байтам — вместе с SaaS-слоем.
* Named workspace — это состояние вне git-трёх-flow: изменения в нём не
  версионируются и не ревьюятся, если агент сам не сделает его репозиторием.
  Принято осознанно как «рабочий стол» тенанта: путь к проверяемости остаётся
  прежним (repo/push), а полный audit trace действий агента сохраняется в
  любом случае.
* `TaskRunner` получает cooperative-cancel — единственное вторжение в
  runtime; остальное стоит на уже существующих швах (`resume_run`,
  `provision.py`, `TenantHub`).
* Remote-CLI дублирует поверхность API в клиентском коде — цена за UX
  «как claude, только сервер исполняет»; сдерживается тонкостью маппинга
  (без бизнес-логики на клиенте).

## Связано

ADR-0012 (tenant = agent-home), ADR-0013 (роли), ADR-0014 (интеграция
мультитенантности — auth, hub, квоты), ADR-0003 (Flow C — клон продолжает
его же), ADR-0002/0006 (credentials только host-side, redaction),
ADR-0005 (resume/supervisor), ADR-0015 (hardened git), ADR-0016 (GC-паттерн).
