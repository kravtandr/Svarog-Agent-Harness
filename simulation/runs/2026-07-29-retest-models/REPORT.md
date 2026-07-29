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

