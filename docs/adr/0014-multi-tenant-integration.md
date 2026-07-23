# ADR-0014: Интеграция мультитенантности на hardened-docker per-run

## Статус

Принято, фазы 1–3 реализованы (`src/svarog_harness/tenant/`, `gateway/hub.py`)

## Контекст

ADR-0012 задаёт модель (tenant ≡ agent-home), ADR-0013 — роли (superuser /
standard). Субстрат исполнения выбран: **hardened-docker per-run** (не
Kubernetes) — изоляция standard-пользователя держится контейнером
(`--network none`, `cap-drop ALL`, non-root, mount только своего
workspace+skills), а k8s/microVM остаются потолком масштаба (см. обсуждение
топологий, отклонено на данном этапе). Этот ADR фиксирует **конкретную
интеграцию** в существующий код: где именно проходят швы и какие развилки
приняты.

Ключевое свойство кодовой базы, на котором стоит вся интеграция: ядро прогона
(`TaskRunner` и ниже) уже полностью параметризовано через `cfg`/`workspace` —
`secrets.path`, `storage.db_path`, `skills.paths`, `sandbox`, `memory.path`
берутся из cfg. Значит, изоляция достигается **резолвингом per-tenant cfg**, а
не правкой ядра.

## Принятые развилки

1. **Auth gateway/Telegram — per-tenant bearer token.** У каждого тенанта свой
   статичный токен в его SecretStore; реестр маппит `token → tenant`. Резолвер
   спрятан за интерфейсом `TenantContext`, чтобы позже добавить JWT без правки
   роутов.
2. **LLM-ключ провайдера — глобальный общий.** Один ключ на харнесс,
   инжектится host-компонентом; тенанты его имени/значения не видят. Tenant
   `secrets.json` — только для пользовательских секретов задачи.
3. **Resume/супервизор — control-plane индекс `run_id → tenant_id`.** Пишется
   на старте run; `svarog resume <id>` и refuel-супервизор находят тенанта сами.
   UX resume не меняется; `--tenant` — необязательный override.

## Решение: швы интеграции

### Слой резолвинга (единственная обязательная новая операция)

`config/paths.py` получает две чистые функции; через них проходит любой запуск:

```python
def resolve_tenant_config(base: SvarogConfig, home: Path, role: Role) -> ResolvedTenant:
    cfg = base.model_copy(update={
        "memory":  base.memory.model_copy(update={"path": home / "memory"}),
        "skills":  base.skills.model_copy(update={"paths": [home / "skills", *shared_ro(base)]}),
        "storage": base.storage.model_copy(update={"db_path": home / "svarog.db"}),
        "secrets": base.secrets.model_copy(update={"path": home / "secrets.json"}),
    })
    cfg = clamp_by_role(cfg, role)      # принуждение роли (ниже)
    assert_confined(cfg, home)          # все пути строго под home/, иначе — ошибка
    return ResolvedTenant(cfg=cfg, workspace=home / "workspaces", role=role)

def clamp_by_role(cfg: SvarogConfig, role: Role) -> SvarogConfig:
    if role != "standard":
        return cfg
    # Кламп сильнее per-tenant yaml: standard не исполняется на хосте и не может
    # ослабить безопасность конфигом (сеть/скан) до старта run (ADR-0013).
    return cfg.model_copy(update={
        "sandbox":  cfg.sandbox.model_copy(update={"type": "docker", "network": "disabled"}),
        "secrets":  cfg.secrets.model_copy(update={"env_fallback": False}),
        "git":      cfg.git.model_copy(update={"secret_scan_before_commit": True}),
        "verifier": cfg.verifier.model_copy(update={"secret_scan": True}),
    })
```

`TaskRunner(resolved.cfg, resolved.workspace)` — дальше ядро без изменений.

**Инвариант mount-scope.** В контейнер монтируется только конкретный
task-workspace (`home/workspaces/<task>`), никогда `home/` целиком — иначе
соседние `memory/`, `secrets.json`, `svarog.db` попадут в sandbox и пробьют
ADR-0002 (память/секреты недоступны) и ADR-0004 (запись минуя очередь). Это
enforcement-инвариант docker-backend'а, а не настройка (см. ADR-0013).

### Два скоупа секретов (развилка 2 + фикс env-leak)

Секреты разведены на два независимых стора — иначе «глобальный LLM-ключ» и
«file-only для standard» исключают друг друга:

* **Глобальный/host-стор** — `provider.api_key_ref`, `gateway.token_ref`.
  Резолвится **host-side** и используется в host-процессе loop'а (LLM-вызов
  идёт с хоста, не из контейнера) — в sandbox не попадает (ADR-0002/0006).
* **Tenant-стор** — задачные секреты (`secrets.inject`, выданные tool'у по
  approval). Для standard = **только** `FileSecretStore` своего тенанта, без
  env-fallback (`get(ref)` не проваливается в хостовый `os.environ`).
  Инжектятся только в **его** контейнер, когда явно выданы (ADR-0006).

`provider.api_key_ref` резолвится против глобального стора, tenant-ref — против
tenant-стора; пути не пересекаются.

### Gateway → TenantHub

`GatewayService` остаётся per-tenant, их держит хаб:

```python
class TenantHub:
    def service_for(self, tenant_id: str) -> GatewayService:  # ленивый кеш dict[id, svc]
```

`api.py`: `_auth_dependency` из «сверить один токен» → `Depends`, возвращающий
`TenantContext(tenant_id, role)` по `token → registry`. Роут:
`ctx = auth(); hub.service_for(ctx.tenant_id).create_run(...)`.

**Refuel-супервизор (ADR-0005) — per-tenant.** Сейчас `auto_resume_refuel`
сканирует одну БД; в мультитенанте он проходит по control-plane run-index
(`run_index`), берёт `suspended`-run'ы с их `tenant_id`, и поднимает каждый
через `hub.service_for(tenant_id).resume(run_id)`. Так не нужно открывать N БД
вслепую, а `refuel_pending`-фильтр из ADR-0005 сохраняется per-run.

### Telegram

`_authorized(user_id)` → `registry.resolve(f"telegram:{user_id}")`; нет записи —
отказ (сохраняем «интернет-facing без allowlist опасен»). Далее
`hub.service_for(ctx.tenant_id)`. Роутинг `chat_id → run` не трогаем.

### CLI

* Дефолт — тенант `local` (superuser, home = текущий agent-home), поведение как
  сейчас при `tenancy.enabled=false`.
* `--tenant <id>` на `run`/`chat`/`resume` для явного выбора.
* `svarog tenant create|list|add-principal|token` — control-plane.

### Reg­istry и провижн

`tenant/registry.py` — MVP-бэкенд `agent-home/tenants.json` под
`LockBackend`-гардом (паттерн «файл сейчас, pluggable потом», как
locks/secrets/storage). Хранит `tenants[id] = {role, created_at, principals,
quotas}` и обратный индекс `principal → id` + `run_index[run_id] = id`.

`svarog tenant create <id> --role standard` переиспользует существующую
init-логику (`init_git_subrepo`, `init_db`):
1. `registry.reserve(id)` под локом;
2. дерево `tenants/<id>/{memory,skills,workspaces,policies}`;
3. `git init` для `memory/` и `skills/` (Flow A/B);
4. `init_db(home/svarog.db)`;
5. `secrets.json` 0600;
6. выпуск per-tenant bearer-token → tenant-секреты + индекс principal'ов;
7. `registry.commit` (rollback дерева при частичном фейле).

### Fail-closed для standard

На старте run: `role == "standard" and find_docker() is None` → **отказ** (не
откат в `local-trusted`). Гард в границе тенанта перед `TaskRunner.run_once`.

### MCP-серверы — per-tenant

`cfg.mcp.servers` сейчас глобальны: общий процесс сервера и общие `env_refs`
-секреты на всех тенантов — кросс-тенантная щель. В мультитенанте MCP
резолвится per-tenant (`env_refs` — против tenant-стора), а для роли `standard`
MCP по умолчанию **выключен** (opt-in per-tenant): внешний сервер — это выход
за пределы sandbox, который standard не должен получать без явного разрешения.

## Конфигурация

```yaml
tenancy:
  enabled: false                       # false → один неявный tenant "local" (superuser), поведение как сейчас
  home_root: ./agent-home/tenants
  default_role: standard
  provisioning: manual                 # | first_touch
  shared_skills: [./agent-home/skills] # ro-слой под tenant-скиллами
```

## Инварианты (тесты — часть Definition of Done)

* **Confinement**: все резолвнутые пути под `tenants/<id>/`; `..`/symlink → ошибка.
* **Mount-scope**: контейнер видит только `home/workspaces/<task>` + skills-ro;
  тест, что `memory/`/`secrets.json`/`svarog.db` НЕ смонтированы (нет `home/` целиком).
* **Кламп сильнее конфига**: standard всегда `sandbox.type=docker`, `network=disabled`,
  secret-scan включён — что бы ни стояло в per-tenant yaml.
* **Fail-closed**: standard без docker → отказ, не local-trusted.
* **Frozen role**: роль/sandbox не эскалируются изнутри run (как autonomy,
  ADR-0010; тест на prompt-injection).
* **Host-side file-tools**: у standard `Read/Write/Edit` идут host-side; тест
  кросс-тенантного escape'а через `resolve_in_workspace` (docker сюда не защищает).
* **Кросс-тенант**: A не резолвит/не resume'ит run B (auth); не видит файлы B
  (bash — docker mounts; file-tools — confinement); не читает секреты B (file-only store).
* **Env-leak**: standard-ref не резолвится в хостовую env; `provider.api_key_ref`
  берётся из глобального стора, а не из tenant-стора.

## Фазы реализации (статус)

* **Фаза 1 — ✅ выполнено.** `config/paths.py`
  (`resolve_tenant_config`/`clamp_by_role`/`assert_confined`/`resolve_local_tenant`/
  `tenant_home`/`registry_path`), `tenant/` (`registry.py` — JSON+flock,
  `models.py`), `SecretsConfig.env_fallback`, `TenancyConfig`, дефолтный
  `local`-тенант. Ядро не тронуто; инварианты-тесты (`tests/test_tenant.py`).
* **Фаза 2 — ✅ выполнено.** `gateway/hub.py` (`TenantHub` + `SingleTenantResolver`
  за протоколом `GatewayResolver`), per-tenant bearer-auth в `gateway/api.py`,
  Telegram-резолвинг (`TelegramBot.from_hub`), `tenant/provision.py` +
  `svarog tenant create|list|add-principal|token`, `run_index` (колбэк
  `on_run_created`) + resume-роутинг + per-tenant refuel-супервизор,
  fail-closed гард (`TaskRunner.assert_sandbox_available`), role re-clamp на
  resume, serve-wiring. Тесты: `test_tenant_gateway.py`, `test_tenant_provision.py`.
* **Фаза 3 — ✅ выполнено.** Квоты (`tenant/quota.py`, `QuotaConfig`,
  enforcement на `create_run` → HTTP 429), JWT-бэкенд (`gateway/jwt_auth.py`,
  stdlib HS256, роль из реестра), first-touch provisioning (Telegram).
  Тесты: `test_tenant_quota.py`, `test_tenant_jwt.py`.
* **Находки ревизии — ✅ закрыты.** #2 два секрет-скоупа
  (`TaskRunner._host_store` для provider/MCP/gateway host-side vs `_store`
  для sandbox-инъекции); #8 MCP выключен для standard клампом.

**Осталось (пост-MVP, вне этого ADR):** per-tenant config-layering (opt-in MCP
для standard, per-tenant `svarog.yaml`-оверрайды); ротация `run_index`;
shared-Postgres scale-бэкенд с колонкой `tenant_id`.

## Последствия

* Ядро (`TaskRunner` и ниже) не меняется — только резолвинг, реестр и точка
  мультиплекса в интерфейсах.
* **Память per-tenant**: очередь `MemoryChangeRequest` и writer-лок живут в
  per-tenant `db_path` → single-writer (ADR-0004) действует на репо тенанта
  (строго сильнее). Цена: N тенантов = N idle writer-задач и N secret-файлов.
* **Натяжение с ADR-0007**: «multi-process без правки кода, только backends»
  держится для per-tenant SQLite, но переход на shared-Postgres (пост-MVP)
  требует колонки `tenant_id` — это **схемное** изменение, не только конфиг.
  Принято осознанно как scale-путь за пределами этого ADR.
* Глобальный LLM-ключ упрощает биллинг, но не даёт per-tenant разделения
  расходов у провайдера — учтено в квотах (Фаза 3): бюджеты cost/tokens на тенанта.
* Per-tenant bearer-токены требуют ручной ротации (`svarog tenant token --rotate`);
  JWT-бэкенд (Фаза 3) даёт stateless-токены с TTL за тем же `GatewayResolver`.
* Control-plane реестр и `run_index` — общий writer поверх тенантов (под
  `LockBackend`-гардом): неизбежная для мультитенанта разделяемая точка, но
  маленькая и низкочастотная.

## Связано

ADR-0012 (модель тенанта), ADR-0013 (роли), ADR-0002 (enforcement над
классификацией), ADR-0010 (заморозка режима), ADR-0004 (single-writer —
теперь per-tenant), ADR-0006 (секреты), ADR-0007 (pluggable storage).
