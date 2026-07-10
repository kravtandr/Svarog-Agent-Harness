# ADR-0013: Два уровня привилегий — superuser (хост) и standard (sandbox-only)

## Статус

Предложено

## Контекст

ADR-0012 вводит тенантов с изолированными agent-home. Поверх изоляции *данных*
нужна ось *привилегий исполнения*: часть пользователей — доверенные операторы
хоста (им нужен полный доступ к машине, произвольным репозиториям, хостовым
секретам), а часть — обычные пользователи, которые должны работать **только**
внутри sandbox, без доступа к файлам хоста и файлам других тенантов.

Механизмы принуждения для этого уже существуют и их не нужно изобретать:

* **Sandbox-бэкенды** (§6.9, ADR-0002): `docker` — hardened-контейнер (сеть
  выключена, `cap-drop ALL`, non-root, ФС ограничена явными mounts: `/workspace`
  rw + `/skills` ro); `local-trusted` — исполнение прямо на хосте «без гарантий»
  (§17). Это ровно граница «sandbox-only» vs «доступ ко всему».
* **file-tools** уже конфайнят пути в workspace host-side
  (`resolve_in_workspace`: `resolve()` + `is_relative_to`).
* **Host-компонент** (ADR-0002/0006) выполняет операции с credentials (git
  push/pull, инъекция секретов) вне sandbox.
* **Фиксация при старте run** (ADR-0010): autonomy и policy замораживаются в
  конструкторе; эскалация изнутри run невозможна — защита от prompt injection.

Задача ADR — связать роль пользователя с этими слоями так, чтобы изоляция
standard-пользователя держалась на **уровне 1** (sandbox), а не на доброй воле
policy (принцип ADR-0002: enforcement over classification).

## Решение

### 1. Роль — свойство principal'а, не run'а

В `TenantRegistry` (ADR-0012) у каждого principal/тенанта — поле:

```python
role: Literal["superuser", "standard"]   # по умолчанию "standard"
```

Роль резолвится из **аутентифицированного** principal'а при старте run и
замораживается вместе с autonomy/policy (ADR-0010). Ни аргументы задачи, ни
per-tenant `svarog.yaml`, ни сам агент не могут её поднять — эскалация изнутри
run невозможна по построению. Выдать `superuser` может только другой superuser
или хостовый admin-CLI; регистрация нового пользователя всегда даёт `standard`.

### 2. Роль → профиль исполнения (точка принуждения)

Резолвер выводит из роли профиль и **клампит** им конфиг, перекрывая любые
per-tenant значения:

| | superuser | standard |
|---|---|---|
| Sandbox | `local-trusted` разрешён (доступ к хосту) | `docker` **принудительно** |
| Mount контейнера | ФС хоста доступна (local-trusted) | **только `home/workspaces/<task>` rw + `/skills` ro**; НИКОГДА не `home/` целиком |
| Сеть sandbox | по конфигу | `disabled` **принудительно** |
| Secret scan (commit/push) | по конфигу | включён **принудительно** |
| file-tools (host-side) | конфайн в workspace (хост-репо — через bash) | конфайн в **свой** tenant-workspace |
| Host-ops (git push/pull, private) | произвольные репозитории хоста | только remotes своего тенанта |
| Секреты | глобальный/host-стор + свои | **только** свой `secrets.json` (env-fallback выключен) |
| Policy-профиль | широкий (critical-set остаётся) | строгий overlay поверх critical-set |
| Out-of-workspace ops | разрешены | `data.delete_outside_workspace` и т.п. — deny |

```python
def clamp_by_role(cfg: SvarogConfig, role: Role) -> SvarogConfig:
    if role != "standard":
        return cfg  # superuser — как настроено
    # Принуждение уровня 1: standard НИКОГДА не исполняется на хосте и не может
    # ослабить безопасность через свой per-tenant yaml (кламп сильнее конфига).
    return cfg.model_copy(update={
        "sandbox":  cfg.sandbox.model_copy(update={"type": "docker", "network": "disabled"}),
        "secrets":  cfg.secrets.model_copy(update={"env_fallback": False}),
        "git":      cfg.git.model_copy(update={"secret_scan_before_commit": True}),
        "verifier": cfg.verifier.model_copy(update={"secret_scan": True}),
        # + строгий policy-overlay (out-of-workspace deny) поверх critical-set
    })
```

**Инвариант mount-scope (критично).** В контейнер монтируется **только**
конкретный task-workspace (`home/workspaces/<task>`), никогда `home/` целиком:
рядом с `workspaces/` лежат `memory/`, `secrets.json`, `svarog.db` — их
попадание в mount разом пробьёт ADR-0002 (память/секреты недоступны в sandbox)
и ADR-0004 (прямая запись в память минуя очередь). Это enforcement-инвариант,
не настройка.

### 3. Fail-closed для standard

Если для standard-пользователя docker/podman недоступен (`find_docker()` →
None) — run **отклоняется**, а не откатывается в `local-trusted`. Тихий
фолбэк на хостовое исполнение пробил бы изоляцию, поэтому его нет: отсутствие
sandbox = отказ, а не понижение гарантий (ADR-0002, fail-closed).

### 4. Почему изоляция держится на уровне 1

Для standard-пользователя граница — **две разные механики**, а не policy:

* **bash — слой-1 (docker).** Исполняется в контейнере, где по инварианту
  mount-scope смонтированы **только** его task-workspace и skills-ro; хостовой
  ФС и каталогов других тенантов там физически нет (`--network none` вдобавок
  отрезает сеть). Это гарантия ADR-0002.
* **file-tools — host-side confinement, НЕ docker.** `Read/Write/Edit`
  исполняются в host-процессе loop'а (не в контейнере) и конфайнятся
  `resolve_in_workspace` в tenant-workspace. Значит для standard файловая
  изоляция tool'ов держится **только** на этом confinement'е — он обязан быть
  привязан к tenant-workspace и покрыт тестом кросс-тенантного escape'а. Не
  путать с изоляцией bash: docker сюда не защищает.
* **Host-компонент** (memory-writer, SecretStore, git-credentials) работает по
  per-tenant cfg (ADR-0012 path-confinement) — только под `tenants/<id>/`.

Policy для standard — **третий**, вторичный слой (defence-in-depth): даже сняв
все policy-правила, пользователь не выйдет за контейнер (bash) и свой workspace
(file-tools). Это и есть требование ADR-0002.

### 5. Взаимодействие с autonomy

Роль и autonomy (ADR-0010) ортогональны, но связаны верхней границей: standard
может работать в `yolo`, оставаясь запертым в sandbox (yolo управляет тем, *что*
делается без approval, роль — *где* это исполняется и до чего дотягивается).
Superuser в `yolo` + `local-trusted` = текущий доверенный CLI-оператор.
`policy.weaken` и остальной critical-set требуют approval при любой роли.

## Последствия

* Изоляция обычного пользователя — свойство контейнера, а не конфигурации: её
  нельзя ослабить ни через промпт, ни через per-tenant yaml (кламп сильнее).
* Standard-пользователи требуют работающего container-runtime; без него — отказ.
  Это осознанная цена fail-closed.
* Superuser остаётся полноценным «хостовым» режимом — обратная совместимость с
  нынешним `local-trusted` CLI (это и есть роль superuser у `cli:local`).
* Хостовые/глобальные секреты нельзя инжектить в standard-sandbox — иначе утечка
  через контейнер; для standard доступен только его SecretStore.
* Роль добавляет одно поле в реестр и один кламп в резолвинге; ядро (`TaskRunner`
  и ниже) не меняется — оно уже параметризовано sandbox/policy/paths из cfg.

## Альтернативы

1. **Различать привилегии только policy-правилами** (без принудительного
   sandbox). Отклонено: противоречит ADR-0002 — тогда изоляция держится на
   классификации, и один пропущенный deny (или prompt injection, склонивший к
   опасной команде) открывает хост.
2. **Роль как параметр запроса/конфига run'а.** Отклонено: даёт
   самоэскалацию, ломает модель ADR-0010. Роль обязана приходить из
   аутентифицированного principal'а.
3. **Отдельный ОС-пользователь/VM на роль.** Сильнее, но тяжелее в
   эксплуатации; docker-контейнер per-run + host-компонент уже дают нужную
   границу. Оставлено как возможный Фаза-3 hardening.
4. **Разрешить standard фолбэк в local-trusted, когда docker недоступен.**
   Отклонено — это и есть дыра, ради закрытия которой вводится роль.
