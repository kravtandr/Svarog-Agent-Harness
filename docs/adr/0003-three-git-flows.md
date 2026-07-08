# ADR-0003: Три Git-flow — память, скиллы, рабочий код

## Статус

Принято

## Контекст

ТЗ изначально описывало один Git Workspace Manager с одним правилом «work in branch, push after approval». Но под этим скрываются три разных типа репозиториев с несовместимыми требованиями:

* **память** — обновляется часто и автоматически; branch+review на каждое обновление `long_term_memory.md` убивает идею памяти;
* **скиллы** — production-навыки; silent mutation запрещена принципом self-improvement через review (§3.9);
* **код пользователя** — классический рабочий процесс с ветками и push после approval.

## Решение

Три отдельных flow с разными компонентами и policy:

| | Flow A: память | Flow B: скиллы | Flow C: код пользователя |
|---|---|---|---|
| Путь | `agent-home/memory` | `agent-home/skills` | внешние репозитории |
| Коммиты | прямые, в main | только через proposal-ветку | в task branch |
| Кто коммитит | единственный writer-процесс (ADR-0004) | агент в ветку proposal | агент в task branch |
| Review | нет | обязателен (diff + checks + approval) | по правилам репозитория |
| Push | фоновый, без approval | merge после approval | task branch — по autonomy profile (в `yolo` авто + notify); protected ветки — всегда approval |
| Rollback | git revert по истории | revert merge | branch/revert |

Policy Engine различает flow по пути репозитория. Прямой коммит агента в `skills/` (минуя proposal) — deny на уровне policy.

Git-операции с credentials (push, private pull) выполняет host-компонент, не sandbox (ADR-0002).

## Последствия

* Git Workspace Manager разделяется на три компонента: `MemoryRepo`, `SkillRepo`, `WorkspaceRepo` с общей низкоуровневой git-оберткой.
* agent-home может быть одним физическим репозиторием с разными правилами по путям, либо двумя (memory отдельно) — решается при реализации; policy-модель одинакова.
* В MVP Flow B реализуется вручную (агент создает ветку, человек мержит обычным git); автоматизация governance — пост-MVP.
