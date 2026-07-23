# ADR-0012: Мультитенантность — tenant = отдельный agent-home

## Статус

Принято и реализовано (`src/svarog_harness/tenant/`)

## Контекст

Сейчас харнесс однотенантный. `SvarogConfig` держит по одному значению на всё
приложение: `memory.path`, `skills.paths`, `storage.db_path`, `secrets.path`,
один `workspace`. `TaskRunner(cfg, workspace)` — ядро прогона — берёт все пути
из этого единственного cfg; `GatewayService` держит один `TaskRunner`.
Telegram-адаптер имеет `allowed_users: list[int]`, но это **контроль доступа**,
а не изоляция: все прошедшие allowlist пользователи пишут в одну и ту же память,
видят одни и те же скиллы, делят один workspace и одну SQLite-базу (§16 говорит
об одновременной работе через CLI/Telegram/Web, но не о разделении состояния
между людьми).

Нужен режим, где регистрация пользователя создаёт для него изолированный
agent-home: своя память (Flow A), свои скиллы (Flow B), свои workspaces
(Flow C), свои секреты и своя трасса. При этом нельзя ломать однотенантный
CLI-сценарий и нельзя дублировать ядро агента (repo-structure: `cli`/`gateway`
→ `runtime`).

## Ключевое наблюдение

Все пути, различающие тенантов, уже сосредоточены в cfg и разрешаются чистыми
функциями `config/paths.py`. Значит, изоляция достигается **не** переписыванием
ядра, а введением per-tenant `SvarogConfig` с переписанными путями. Ядро
(`TaskRunner`, `MemoryWriter`, `SecretStore`, `default_lock_backend`,
trace-recorder) остаётся нетронутым — оно уже параметризовано по cfg.

## Решение

### 1. Модель: tenant ≡ agent-home

Тенант — это разрешённый в абсолютные пути agent-home. Каждый
зарегистрированный пользователь получает поддерево:

```
agent-home/
  tenants.json                 # control-plane: реестр тенантов и principal→tenant
  skills/                      # (опц.) общие скиллы, read-only, слой под tenant-скиллами
  svarog.yaml                  # базовый конфиг: providers, models, дефолты (общий слой)
  tenants/
    <tenant_id>/
      memory/                  # Flow A: свой repo, свой single-writer, своя очередь
      skills/                  # Flow B: свои скиллы и proposals
      workspaces/tasks/        # Flow C: task-воркспейсы этого тенанта
      secrets.json             # 0600, свой SecretStore (ADR-0006)
      svarog.db                # своя SQLite: runs, trace, memory-queue, locks (ADR-0007)
      policies/                # (опц.) per-tenant policy-оверрайды
      svarog.yaml              # (опц.) per-tenant оверрайды поверх базового
```

### 2. Резолвинг тенанта — чистая функция

Расширяем `config/paths.py` функцией, которая берёт базовый cfg и home тенанта
и возвращает per-tenant cfg с переписанными путями:

```python
def resolve_tenant_config(base: SvarogConfig, home: Path) -> SvarogConfig:
    """base (общий слой) + пути, привязанные к agent-home тенанта."""
    return base.model_copy(update={
        "memory":  base.memory.model_copy(update={"path": home / "memory"}),
        "skills":  base.skills.model_copy(update={
            "paths": [home / "skills", *shared_skill_dirs(base)]}),  # tenant rw + shared ro
        "storage": base.storage.model_copy(update={"db_path": home / "svarog.db"}),
        "secrets": base.secrets.model_copy(update={"path": home / "secrets.json"}),
    })

def tenant_workspace(home: Path) -> Path:  # workspace-root для TaskRunner
    return home / "workspaces"
```

Инвариант изоляции: все разрешённые пути обязаны лежать под `tenants/<id>/`;
валидатор отклоняет выход за пределы (`..`, symlink-escape) — переиспользуем
confinement, который и так нужен file_tools.

### 3. Идентичность и реестр (control plane)

`TenantRegistry` поверх маленькой общей control-БД (или `tenants.json`):

* запись тенанта: `tenant_id → {display_name, created_at, principals[], quotas}`;
* индекс principal → tenant_id. **Principal** — типизированный идентификатор
  из интерфейса:
  * `telegram:<user_id>` — из `message.from.id`;
  * `gateway:<subject>` — из per-tenant bearer-token или `sub` JWT;
  * `cli:local` — маппится на дефолтного тенанта.

Регистрация (`svarog tenant create <id>` / admin-endpoint gateway):

1. создать дерево каталогов; `git init` для `memory/` и `skills/` (Flow A/B);
2. выпустить per-tenant gateway-token, положить в SecretStore;
3. добавить principal-маппинг в реестр.

Опция first-touch provisioning: неизвестный, но разрешённый пользователь
триггерит авто-создание тенанта (конфигурируемо), иначе — отказ, как сейчас.

### 4. Мультиплексирование в runtime

Gateway держит ленивую карту `dict[tenant_id, TaskRunner]` (или
`GatewayService` на тенанта). Каждый `TaskRunner` тенанта владеет **своей**
asyncio-задачей memory-writer и **своим** SQLite-локом. Тем самым
single-writer из ADR-0004 действует per-tenant memory-repo — это строго
сильнее: между тенантами нет ни контенции, ни разделяемого состояния.
Telegram/gateway сначала резолвят principal → tenant_id, затем берут runner
тенанта; `create_run` получает tenant-контекст, а не глобальный.

### 5. Слоение конфига и скиллов

* Конфиг: базовый `agent-home/svarog.yaml` (провайдеры, модели, дефолты) —
  общий; per-tenant `svarog.yaml` накладывается существующим `deep_merge`
  (loader уже умеет цепочку user→project→env — добавляется tenant-слой).
  LLM-провайдеры и API-ключи остаются централизованными; расходятся только
  per-tenant ручки (квоты, дефолтная autonomy, набор скиллов).
* Скиллы: `skills.paths` становится слоёным — `[tenant (rw через proposal),
  shared (ro)]`. `scan_skills`/`first_existing_skills_dir` уже итерируют список,
  так что слой добавляется без изменений логики. Curator (ADR-0009) гоняет
  per-tenant по tenant-скиллам.

### 6. Безопасность и sandbox

* Секреты per-tenant (ADR-0006) — нет утечки между тенантами.
* Policy Engine не меняется по форме: он различает flow по пути repo (ADR-0003);
  пути теперь с tenant-префиксом — логика та же. Per-tenant policy-оверрайды опц.
* Sandbox (ADR-0002): mount'ы per-tenant — skills ro из home тенанта, workspace
  rw из его workspaces; ни один writable-mount не разделяется между тенантами.

## Фазы

* **Фаза 1** — модель тенанта, `resolve_tenant_config`, `TenantRegistry`,
  мультиплекс в gateway, маппинг principal'ов. Дефолтный тенант = текущий
  `agent-home` (обратная совместимость).
* **Фаза 2** — provisioning (CLI/admin-endpoint), per-tenant квоты и
  cost-cap'ы, first-touch авто-создание.
* **Фаза 3** — scale-бэкенд: общий Postgres с колонкой `tenant_id` вместо
  N SQLite (ADR-0007 уже допускает pluggable storage).

## Обратная совместимость

Однотенантность = частный случай: дефолтный тенант `local`, чей home — текущий
`agent-home` в корне, **без** переписывания путей. Мультитенантность включается
опциональной секцией `tenancy` в конфиге. CLI остаётся однотенантным
(`cli:local` → `local`), если не передан `--tenant`.

## Последствия

* N тенантов = N SQLite-баз, N memory-writer задач, N secret-файлов — приемлемо
  на умеренном масштабе; на большом — путь к shared-DB (Фаза 3).
* Диск: у каждого home полноценные git-репозитории памяти и скиллов.
* Кросс-тенантный обмен (общие скиллы/знание) — только через явный shared-слой;
  намеренно opt-in ради изоляции.
* Control-plane реестр — новый общий writer, но маленький и низкочастотный.
* Шов чистый: ядро (`TaskRunner` и ниже) не трогается — меняются только
  разрешение путей, реестр и точка мультиплекса в интерфейсах.

## Альтернативы

1. **Один agent-home с колонкой `tenant_id`** и путями-префиксами. Меньше
   изоляции, шире blast-radius, тяжелее git-per-tenant. Отклонено для старта,
   но это и есть Фаза 3 для масштаба.
2. **Процесс/контейнер на тенанта.** Максимальная изоляция, тяжёлая
   эксплуатация. Избыточно: in-process мультиплекс с per-tenant DB/каталогами
   достаточен и совпадает с формулой ADR-0004 «один механизм для одного и
   нескольких процессов».
3. **Изолировать только память и скиллы, разделяя workspace/db.** Протекает
   история run'ов между тенантами. Отклонено.
