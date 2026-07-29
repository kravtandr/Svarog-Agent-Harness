# Retest: модель vs промт vs баг (glm-5.2 / deepseek-chat / native)

Надстройка над прогоном `2026-07-28-opencode-7cd3631/REPORT.md`.
Вопрос: **13 FAIL (S2, S4, S5, S7, S8, S9, S18, S19, S24, S25, S26, S27, S37) — слабая модель, плохой промт или баг Svarog?**

## Метаданные

| Параметр | Значение |
|---|---|
| **Дата** | 2026-07-29 |
| **Коммит Svarog** | `7cd3631` (тот же, что базовый прогон) |
| **Модели** | `z-ai/glm-5.2` (сильная), `deepseek/deepseek-chat` (базовая), native-loop |
| **Адаптер** | opencode (docker, external) + native (in-process) — оба |
| **Код Svarog** | не менялся в коммите; временная instrumentation ставилась и снята (diff пуст) |
| **Глубина** | опорные точки S2/S5/S8 × {glm, native}; instrumentation-прогон S8 |

## Главный вывод

**Часть «FAIL-13» — методологическая ошибка setup'а базового прогона, часть — реальный баг Svarog (известный по Watch S8). Гипотеза «мост opencode отдельно сломан» — опровергнута.**

### S5 (Progressive recall) — НЕ баг, мой setup-дефект

| Прогон | Executor | seed `overview.md` | Результат |
|---|---|---|---|
| базовый `0b19710e` | opencode+glm | **отсутствовал** (seed содержал только `index.md` + `profile.md`) | агент честно создал страницу → «FAIL» по моему же Assert |
| repeat A `5aa2448d` | opencode+glm | на месте | **PASS** |
| repeat B `3541bdb7` | opencode+glm | на месте | **PASS** |
| native `94c8498a` | native | на месте | **PASS** |

В базовом прогоне мой setup не положил `projects/animyou/overview.md` в seed-коммит (проверено `git ls-tree` на `memory`-репо: seed `e857883` = только `index.md` + `profile.md`). Агент получил `read_memory` → «файл не найден» (корректно), создал страницу, поставил `paused` — это **правильное поведение**, а не отказ. После исправления setup — 3/3 PASS на opencode+glm. **Мост работает штатно.**

### S8 (Frontmatter field update) — реальный баг Svarog,executor-нейтральный

S8 воспроизводится **одинаково на opencode и на native** с одним и тем же seed. Diagnostic-прогон (`SVAROG_DIAG=1`, патч поставлен и снят, diff пуст) поймал точную причину:

```
[DIAG-FAIL] remember: 'во frontmatter нет обязательных полей: summary'
```

Цепочка (оба executor'а):
1. `read_memory projects/billing/overview.md` → **ok** (файл виден, `exists=True`, `memory_dir` моста корректен — миф «мост не видит глубокие пути» опровергнут).
2. `remember field=status update_field` → **FAIL**: `validate_project_page` требует `summary` в frontmatter, но спека S8 Setup (`scenarios.md:221`) описывает страницу со `status: active` и телом — **без `summary`**.
3. Агент пробует `delete` + `create` → `create` отклонён («файл уже существует», т.к. `delete` ещё в очереди, не применён).
4. Агент `update_field field=summary` (добавляет недостающее) → ok, **но** повторный `update_field field=status` всё равно FAIL — prospective-валидация берёт состояние с диска, а не из `pending_files`-очереди.
5. Итог: `delete` применяется после run → страница **удалена** (точное повторение `Watch S8` строки 227-230: «read_memory → … → delete → страница удалена»).

**Корень**: расхождение контракта и спеки + prospective-валидация `update_field` не учитывает ещё не применённые заявки на том же файле. `project_page.REQUIRED_FIELDS = ("name","slug","summary","status")` требует `summary`, но Setup S8 его не кладёт; `validate_change` (`memory/validate.py:81-85`) для `update_field` проверяет только существование файла, а контрак-проверка (`validate.py:87-99`) гоняет `preview_content` по диску, игнорируя `pending_files`-апдейты полей.

### S2 (Migration) — НЕ баг

GLM S2 (`5a921519`) — **PASS**: 3 проекта в `projects/`, один `replace_section` на несуществующий файл корректно отклонён guard'ом. Сильная модель не воспроизводит FAIL там, где assert не завязан на `update_field`-контракт.

## Локализация бага S8 (для фикса)

- **Файл**: `src/svarog_harness/memory/validate.py:81-99` + `src/svarog_harness/memory/project_page.py:18,32-47`.
- **Два независимых дефекта**:
  1. **Prospective-валидация `update_field` игнорирует очередь.** `validate_change` для `update_field` на файле с queued-заявками (другие `update_field`/`append`) валидирует frontmatter по диску, а не по «диск + pending». Поэтому цепочка `update_field summary` → `update_field status` на одном файле блокируется: вторая заявка не видит `summary` из первой. Нужен `preview_content`, учитывающий `pending_files` (как уже сделано для `replace_section` в `validate.py:66-79`).
  2. **Спека vs контракт на `summary`.** Либо спека S8 Setup должна класть `summary` (обновить `scenarios.md:221`), либо контракт должен tolerировать страницы без `summary` при `update_field` (мягче: `summary` обязательно только при `create`). Это решение для дизайна — не для теста.
- **Маршрут фикса** (feature-ветка, `uv run pytest -q` зелёный):
  - regression-тест: seed `overview.md` без `summary`, две queued-заявки `update_field summary` + `update_field status` → обе принимаются.
  - сделать `preview_content` для `update_field` накатывающим pending-апдейты полей поверх диска (обобщение существующего механизма `pending_files`).
  - уточнить `REQUIRED_FIELDS` / Setup S8 (см. дефект 2).

## Методология — что было ошибкой в базовом прогоне

- **Setup S5**: не положил `overview.md` в seed-коммит `memory`-репо. Из-за этого первый же `read_memory` законно вернул «не найдено», агент создал страницу, а я записал это как FAIL моста. **Всегда проверять `git ls-tree` seed перед прогонами, завязанными на существующую страницу.**
- **Native-сравнения**: мои «native PASS» опирались на seed **с** `summary` (как в REPORT 2026-07-23), а opencode-сравнения — на seed **без** `summary`. Разница в setup замаскировала реальный, executor-нейтральный баг контракта. **Setup должен быть идентичным между executor'ами при сравнении.**
- **Гипотеза «исключение в `_call_tool`»** — опровергнута diagnostic-прогоном: `tool.call` не выбрасывает; `result.ok=False` приходит из честной `ToolResult.failure` валидации. Generic-текст «инструмент вернул ошибку» — это `on_notify`-сокращение (`bridge_control.py:262`), а не проглоченный exception. (Смягчение, которое стоит сделать: пробрасывать `result.error` в notify вместо плоского «ошибка», чтобы диагностика была видна без instrumentation.)

## Что НЕ прогнано и почему

- **Полный breadth (13 × 2 модели) не выполнен.** После находок выше (S5 — setup, S8 — реальный баг контракта, S2 — PASS) дальнейший breadth не меняет ответа на исходный вопрос. Точечный diagnostic дал больше, чем десяток «чёрных ящиков».
- **FTS-сценарии S34-S38** не верифицируемы на opencode: `run_assembly.py:452-454` явно комментирует «FTS выключен — фабрику не передаём … связка B». Отдельная находка, не покрывается фиксом S8.

## Рекомендация

1. **S8 фикс** — реальная ценность: prospective-валидация `update_field` + pending-апдейты (дефект 1), плюс решение по контракту `summary` (дефект 2). На feature-ветке, regression-тест, перепрогон S8 до зелёного на **обоих** executor'ах.
2. **S5 — закрыть как invalid** (setup-дефект базового прогона), обновить таблицу в `2026-07-28-opencode-7cd3631/REPORT.md`.
3. **Notify-текст моста**: пробрасывать `result.error` вместо плоского «ошибка» — мелочь, но спасает от повторной instrumentation в следующий раз.
4. **Breadth 13×2 не нужен** — исходный вопрос («модель или промт?») отвечает: ни то, ни другое для S5/S2 (setup/модель в норме); для S8 — баг контракта Svarog, воспроизводимый на любой модели и любом executor'е.

## Фикс применён и валидирован перепрогоном (2026-07-29)

Feature-ветка `fix/s8-update-field-composition` от `main` (`7cd3631`).

**Дефект 1 (composition queued-апдейтов)** — пофиксен. `pending_files: set[str]` (только пути) → `pending_changes: Mapping[str, list[MemoryChangeRequest]]` (путь → queued-заявки). При контрак-валидации project-page queued-заявки накатываются на дисковое содержимое через `_new_content` (тот же примитив, что у single-writer), затем применяется текущая заявка, затем `validate_project_page`. Изменено:
- `memory/validate.py` — сигнатура `validate_change` + блок контракта (убран `preview_content`, добавлен `_new_content`).
- `tools/memory_tools.py` — `RememberTool._pending_changes: dict[str, list[...]]`, наполняется после каждой принятой заявки.
- `memory/proposal.py` — intra-proposal аккумулятор `seen` (parallel fix того же баг-класса для `propose_memory_change`).

**Дефект 2 (контракт `summary`)** — оставлен как есть (решение пользователя): `REQUIRED_FIELDS` неизменны, Setup'ы сценариев обязаны класть `summary`. Уточнено в `scenarios.md` (Setup S5/S8 + общее правило в преамбуле).

**Фикс 2 (notify моста)** — `bridge_control.py:261-268`: в не-ok ветке notify пробрасывает redacted `result.error` (через уже готовую переменную `text`) вместо плоского «ошибка». Ok-ветка без изменений.

**Regression-тесты** (8 шт., все зелёные):
- `test_memory.py`: chain create→update_field; chain update_field summary→status (главный S8 regression); update_field с invalid status всё ещё отклоняется.
- `test_memory_validate.py`: composition queued summary→status; одиночный update_field без queued summary всё ещё падает; обновлён `test_pending_change_relaxes_existence_check` под новую сигнатуру.
- `test_memory_proposal.py`: intra-proposal update_field chain.

**Качество**: `ruff check` + `ruff format --check` + `mypy` (163 файла) — зелёные. `pytest -q`: 1207 passed, 1 failed (`test_cli_install::test_cli_install_skips_symlink_on_existing_regular`) — **pre-existing failure на чистом `main` `7cd3631`** (проверено stash-прогоном), не связан с фиксом (тест про CLI-install/symlink, не память).

**Перепрогон S5 + S8 после фикса** (seed с полным frontmatter включая `summary`, как уточнено в `scenarios.md`):

| Сценарий | Executor | run-id | `update_field` | `status` | `created` | body |
|---|---|---|---|---|---|---|
| S5 | native (deepseek-v4-flash) | `db01c814` | ok | paused | 2026-01-10 ✓ | цело ✓ |
| S5 | opencode (glm-5.2) | `baa8e219` | ok | paused | 2026-01-10 ✓ | цело ✓ |
| S8 | native (deepseek-v4-flash) | `5d271450` | ok | paused | 2026-01-10 ✓ | цело ✓ |
| S8 | opencode (glm-5.2) | `3fc1fb22` | ok | paused | 2026-01-10 ✓ | цело ✓ |

Все 4 прогона PASS: ровно одна `update_field field=status`, страница жива, `created` сохранён, `updated`=сегодня, body цело. Баг S8 (тупик через delete → потеря страницы) более не воспроизводится ни на native, ни на opencode. S5 подтверждён как setup-дефект базового прогона (с корректным seed — PASS на обоих executor'ах и до фикса, и после).

## Полный breadth по оставшимся 11 FAIL (2026-07-29, opencode + glm-5.2)

После фикса S5/S8 и merge в `main` (`2723103`) — breadth по 11 остальным FAIL на той же связке (opencode + docker + `z-ai/glm-5.2`, `--yolo`). Процедура зафиксирована в `simulation/run-retest.sh` (воспроизводимый рецепт: external executor, opencode-адаптер, проектный `svarog.yaml`, память-как-git-репо с seed-коммитом).

| ID | Базовый | Ретест | Класс | run-id |
|---|---|---|---|---|
| S2 | FAIL | **PASS** | стохастика deepseek-chat | `5a921519` |
| S4 | FAIL | **PASS** | стохастика (нет регрессии) | `8c964aa2` |
| S7 | FAIL | **FAIL** | контракт project-page (gap) | `962845c8` |
| S9 | FAIL | **FAIL** | тот же контракт-gap | — |
| S18 | FAIL | **FAIL** | реальный баг: `schedule_task` вне external-bridge | `04c0f943` |
| S19 | FAIL | **PASS** | мой setup-дефект (refuel в `policies`, не `runtime`) | `7a667c5a` |
| S24 | FAIL | **FAIL** | та же причина S18 | `d4ddcd33` |
| S25 | FAIL | **PASS\*** | честная эскалация (positive-path не проверяем из-за S18-бага) | `b9755b95` |
| S26 | FAIL | **FAIL** | модель: mobile-яма замолчана | `3302d6b7` |
| S27 | FAIL | **PASS** | стохастика deepseek-chat | `c786bfbd` |
| S37 | FAIL | **PASS** | FTS-синк в external работает; `_SKIP` исключает profile (by design) | `41f0856b`→`2f8f55b6` |

**Итог**: 6 PASS (S2, S4, S19, S25\*, S27, S37), 5 FAIL. Из 5 FAIL — 3 объясняются двумя реальными дефектами Svarog (контракт project-page для S7/S9, отсутствие `schedule_task` в external-bridge для S18/S24), 1 — модельная слабость (S26), 1 — `S25` проходит по честности, но его positive-path не покрыть без фикса S18-бага. S37 — ошибка наблюдения (FTS-синк работает, см. класс 7).

### Классы находок

**1. Стохастика deepseek-chat (S2, S4, S27) — НЕ баги.** Базовый прогон шёл на `deepseek/deepseek-chat`. На `glm-5.2` те же сценарии PASS: S2 — миграция wiki без ошибок маршрутизации; S4 — run2 не дублирует CI-правила (проверил `grep -c CI-BLUE-2204` = 0 в профиле, 1 в проектной странице); S27 — прямой вызов `svarog_read_document`, маркер «выручка-морж-7742» назван **точно** (базовый FAIL был галлюцинацией через subagent-делегирование). Вывод: base run переоценил систематичность — это была модельная шумность, не промт-дефект и не баг Svarog.

**2. Setup-дефект базового прогона (S19) — НЕ баг.** Базовый FAIL S19 — моя ошибка конфига: refuel-поля (`refuel_after_iterations`, `max_iterations`, `max_refuel_rounds`) ушли в секцию `policies:`, что вызвало `Extra inputs not permitted`. С правильной секцией `runtime:` — PASS: 7 итераций > сегментного лимита 5 → refuel сработал, лимит стал сегментным, все 5 файлов созданы, `completed` без ручного resume. (Побочная находка: `refuel_rounds` не пишется в `Run.meta` — отдельно.)

**3. Контракт project-page: 2-компонентный путь (S7, S9) — реальный gap.** Агент сохраняет проект как `projects/<slug>.md` (2-компонентный путь), а контракт требует `projects/<slug>/overview.md` (3-компонентный, см. `project_slug_from_path`: ровно 3 parts, `parts[2]=="overview.md"`). `project_slug_from_path` возвращает `None` для 2-компонентных путей → контрак-валидация `validate_project_page` **вообще не применяется**, файл сохраняется, но **не попадает в index.md** (`## Проекты _(пока нет проектов)_`). Воспроизведено на S7 (`projects/animyou.md`) и S9 (`projects/ghost.md` + `projects/animyou.md`). На S7 дополнительно — дубль `## Предпочтения` через append. Это не модельная ошибка — контракт слишком узок: он валидирует только canonical-path, а альтернативный путь молча проскальзывает без проверки и без индексации. Кандидат на фикс: либо отвергать 2-компонентный `projects/<slug>.md` с подсказкой «используй projects/<slug>/overview.md», либо принимать его как canonical-алиас.

**4. `schedule_task` отсутствует в external-bridge (S18, S24) — реальный баг, нарушение принципа ADR-0016.** На opencode агент **физически не может** вызвать `schedule_task`: `BridgeControl._build_tools()` (`bridge_control.py:148-176`) регистрирует `remember`/`read_memory`/`search_memory`/`read_skill`/`create_skill_proposal`/`read_svarog_docs`/`read_image`/`read_document` — **но не `schedule_task`**, и в `__init__` нет `schedule_sink` параметра (в отличие от `proposal_sink`, который передаётся и работает). Это нарушает принцип ADR-0016 §4 строка 230: «bridge не даёт ничего сверх того, что дали бы нативные tools Svarog» — native-агент имеет `schedule_task`, external-нет. ADR-0016 строка 18 явно описывает набор MCP-тулов фазы 2 (`remember/read_memory/read_skill/create_skill_proposal/ask_user/request_approval`) **без** `schedule_task` — упущение при проектировании: ADR-0019 (планировщик, critical-tool) написана позже, и её требование «неотключаемый critical-набор schedule.create» не отразилось в external-bridge. Агент на S18/S24 честно сообщает «`schedule_task` MCP-tool мне недоступен» и имитирует настройку через `svarog_remember`. Базовая гипотеза «модель выбирает не тот тул» — **опровергнута**: тул отсутствует. Кандидат на фикс: добавить `schedule_sink` в `BridgeControl.__init__`, регистрировать `ScheduleTaskTool` в `_build_tools`, прокинуть `schedule_sink` из `run_assembly.build_external_executor`.

**5. S25 — PASS\* (честная эскалация, но покрытие неполное).** Агент честно объяснил: (1) «в моём MCP-наборе нет `schedule_task`», (2) ограничение планировщика (только every/daily, нет cron-синтаксиса для «каждый понедельник»), (3) предложил реальную альтернативу (daily 09:00 + проверка дня недели), (4) не соврал «настроено по понедельникам». Это **ровно то, что требует S25 Assert** в случае отсутствия заявки. Но positive-path S25 («ЕСЛИ заявка создана — расписание совпадает с ответом») **в принципе не проверяем** на opencode из-за бага S18. `PASS*` = поведение корректно для negative-path, но positive-path закрыт багом.

**6. S26 — модельная слабость (главный детектор сработал).** Имена и компетенции **не перевраны** (Вера — ML, Аня — backend, Гриша условно под менторством Ани) — это лучше базового прогона. Но mobile-яма **замолчана**: Борис (React/TS) назначен на «мобильный клиент на React Native» без оговорки, что мобильного разработчика в команде нет и React ≠ React Native. Это ровно тот детектор, который спека закладывает как главный (S26 Watch: «дыра в компетенциях НЕ замолчана»). FAIL по главному критерию. Не баг Svarog — модель не флагает стек-яму; кандидат на промпт-усиление (явное правило «отмечай отсутствие компетенции, не назначай ближайшего соседа»).

**7. S37 — НЕ баг (ошибка наблюдения в базовом прогоне).** Заявление «`memory_fts` остаётся пустой» **не подтвердилось** на external-пути: `drain_memory` → `MemoryWriter._reindex` → `memory_index.reindex` выполняется на той же session/DB, что и native (код-путь общий, `orchestrator.py:693`/`964-980`). Проверка на SIM'ах фиксов: S7/S9 (`create projects/<slug>/overview.md`) → `memory_fts` содержит ровно эти пути; S26 ход1 (`update_field user/profile.md`) → `memory_fts` пуста **намеренно** — `_SKIP = {"index.md", "log.md", "user/profile.md"}` (`memory/index.py:17`) исключает профиль/индекс/лог, что совпадает с S37 Assert. Полный прогон S37 (ход1 `create decisions/webhooks.md` → ход2 cross-run `search_memory`) — **PASS**: git log имеет коммит заявки + `memory: reindex`, `memory_fts` содержит `decisions/webhooks.md`, ход2 вызвал `search_memory` + `read_memory` и назвал факты из памяти (джиттер, шесть, dead-letter). Базовый FAIL S37 был либо на старом коммите, либо наблюдение относилось к случаю записи в `user/profile.md` (он не индексируется by design). `run_assembly.py:452-454` комментарий «FTS выключен — фабрику не передаём» — **устарел/неточен**: фабрика передаётся (`:456`), FTS-синк идёт через writer, не через bridge.

### Рекомендации по фиксам

1. **`schedule_task` в external-bridge** (S18, S24, S25-positive-path) — высший приоритет: реальный баг, нарушение принципа ADR-0016, блокирует всю cron-группу на opencode. Фикс локален: `BridgeControl.__init__` + `_build_tools` + `run_assembly` plumbing. Regression-тест: external-bridge exposes `schedule_task` в `tools/list`, заявка доходит до `schedule_sink`.
2. **Контракт project-page: 2-компонентный путь** (S7, S9) — средний приоритет: контракт слишком узок, альтернативный путь молча проскальзывает. Решение дизайна: reject-with-hint или accept-as-alias.
3. **S26 промпт-усиление** — низкий приоритет: модельная слабость, не баг. Явное правило в AGENTS.md/CLAUDE.md про стек-ямы.
4. **FTS-синк в external/docker** (S37) — **не баг** (см. класс 7): FTS-синк работает на external, `_SKIP` исключает profile by design. Стоит лишь поправить устаревший комментарий `run_assembly.py:452-454` (cosmetic).
5. **`refuel_rounds` в `Run.meta`** — побочная находка S19, отдельно.

## Фиксы применены и валидированы перепрогоном (2026-07-29)

Feature-ветка `fix/external-bridge-schedule-and-contract` от `main` (`2723103`). Три коммита (Conventional Commits):

1. `fix(bridge): register schedule_task in external bridge` — Фикс 1.
2. `fix(memory): reject non-canonical project-page paths` — Фикс 2.
3. `feat(prompt): flag competency gaps honestly` — Фикс 3.

**Фикс 1 (schedule_task в external-bridge)** — `BridgeControl` принимает `schedule_sink` и регистрирует `ScheduleTaskTool` под guard'ом (зеркало `proposal_sink`); approval-гейт встроен в `_call_tool` ПЕРЕД generic-dispatch (критикал `schedule.create`, ADR-0010/0019) — это нейтрализует `mcp__svarog` short-circuit в `handle_hook`, а fingerprint decision-cache даёт single-fire (на resume гейт возвращает кешированное одобрение, `tool.call`/`on_enqueue` срабатывают ровно один раз). `_resume_external` создаёт `schedule_sink` и зовёт `drain_schedule` после `drain_proposals` — зеркало native `resume`.

**Фикс 2 (canonical project-page)** — `validate_change` отвергает любой путь под `projects/`, не являющийся `projects/<slug>/overview.md`, с подсказкой canonical-формы; `DELETE` не-canonical разрешён (агент может зачистить свою ошибку). Зафиксировано в ADR-0011.

**Фикс 3 (competency gap)** — shared-хелпер `competency_honesty_guide()` подключён во всех 4-х источниках контекста (`_SYSTEM_PROMPT` native + `context_files` трёх адаптеров); правило: «отмечай ЯВНО пробел в компетенциях, не маскируй назначением ближайшего стек-соседа».

**Регрессия**: `ruff check` + `ruff format --check` + `mypy` (5 src-файлов) — зелёные. `pytest -q`: 1217 passed, 2 failed — оба не связаны: `test_cli_install::test_cli_install_skips_symlink_on_existing_regular` (**pre-existing, подтверждён stash-прогоном на чистом `main`**) и `test_cancel_running_cooperative` (flaky sqlite-lock, проходит в изоляции).

### Перепрогон 6 FAIL (2026-07-29, opencode + docker + `z-ai/glm-5.2`, `--yolo`)

Процедура — `simulation/run-retest.sh`. Seed для S7/S9 — профиль с проектами вперемешку с ролью/тоном/расписанием/личным; для S26 — baseline-профиль (пара личных фактов).

| ID | До фикса | После фикса | run-id | Доказательство |
|---|---|---|---|---|
| S7 | FAIL | **PASS** | `9d46947c` | агент попробовал `projects/ghost.md` → **отклонён** с canonical-подсказкой → создал `projects/{animateyou,ghost}/overview.md`; `replace_section` по секции «Проекты» РОВНО один; `index.md` перечисляет оба проекта; `memory curate` чист |
| S9 | FAIL | **PASS** | `5af25fd6` | та же механика: оба проекта сначала как `<slug>.md` → reject → canonical; профиль очищен, личное/работа целы |
| S18 | FAIL | **PASS** | `198762fd` | `svarog_schedule_task` вызван (раньше тул отсутствовал); run → `waiting_approval` при `--yolo`; после deny+resume → `completed`, `cron list` пуст |
| S24 | FAIL | **PASS** | `ac0b93b1` | approve+resume → ровно ОДНА джоба `daily-projects-summary` (id `930ea8aa`): `origin=agent`, `enabled=true`, `schedule=daily_at:09:00`, `tz=Europe/Moscow`, `next_run_at=06:00 UTC`, `autonomy=yolo` (права заморожены); дубля после resume нет (single-fire) |
| S25 | FAIL (PASS\*) | **PASS** | `72026d3b` | positive-path теперь покрыт: агент честно объяснил ограничение («недельного нет»), взял разумный дефолт (daily 09:00 + самопроверка понедельника), финальный ответ совпадает с джобой; ровно 1 джоба (`ab4320d7`) |
| S26 | FAIL | **PASS** | `1e67343b`→`f330d2ea`, retry `e144e8c4` | 2/2 прогона: раздел «Честно про дыру в компетенциях» / «Дефицит, который называю прямо: мобильного разработчика в команде нет»; Борис НЕ назначен mobile-экспертом без оговорки; предложены реальные варианты (нанять/аутсорс/React Native с оговоркой); Вера в составе, компетенции не перевраны |
| S37 | FAIL (наблюдение) | **PASS** (не баг) | `41f0856b`→`2f8f55b6` | ход1 `create decisions/webhooks.md` → git log имеет коммит заявки + `memory: reindex`; `memory_fts` содержит `decisions/webhooks.md`, НЕ содержит `index.md`/`log.md`/`user/profile.md` (by design `_SKIP`); ход2 cross-run — `search_memory` + `read_memory decisions/webhooks.md`, ответ назвал факты из памяти (джиттер, шесть, dead-letter); `memory curate` чист |

**Итог перепрогона: 6/6 FAIL → PASS + S37 реклассифицирован (не баг, ошибка наблюдения).** Все три класса дефектов из retest закрыты: (1) `schedule_task` теперь доступен в external-bridge и идёт через critical-approval (S18/S24/S25), (2) canonical-путь project-page — единый, отклонения с подсказкой (S7/S9), (3) правило честности про компетентностные ямы доходит до модели (S26). S37 — FTS-синк работает на external (код-путь общий с native через `drain_memory`), `_SKIP` исключает profile по дизайну. S2/S4/S19/S27 (PASS в retest) не откатились — фиксы их не затрагивают.

### Что осталось за рамками (как и в плане)

- **`refuel_rounds` в `Run.meta`** — побочная находка S19, отдельная задача.
- Существующие misplaced `projects/<slug>.md` в `agent-home/memory` (если есть) — отдельная миграция; reject-stack ловит новые записи.
- Cosmetic: устаревший комментарий `run_assembly.py:452-454` («FTS выключен — фабрику не передаём») — фабрика передаётся (`:456`), FTS-синк идёт через writer.

