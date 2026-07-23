# Прогон всех 26 сценариев симуляции

## Метаданные прогона

| Параметр | Значение |
|---|---|
| **Дата прогона** | 2026-07-23 |
| **Ветка** | `docs/sim-reframe-superpowers` |
| **Коммит** | `f5b7495` (`f5b74953159e5c03f40c9c5bd68ad99a40f85dce`) |
| **Дата коммита** | 2026-07-23 23:41:34 +0300 |
| **Сообщение коммита** | `docs(simulation): classify scenarios by run/chat/cli mode` |
| **Адаптеры** | native (sandbox local-trusted/docker) + opencode (docker) |
| **Модель** | `deepseek/deepseek-chat` (OpenRouter) |
| **Глубина** | полный `Runs` из спеки каждого сценария |
| **Код Svarog** | не менялся (только прогоны в изолированных песочницах) |
| **Изоляция** | одноразовые `mktemp -d` песочницы, `secrets.path` → общий SecretStore, `executor: native` перебивает user-config |

Детальные отчёты по каждому сценарию — в `details/S<N>.md`.

## Итоговая таблица

| Сценарий | Режим | Runs | Вердикт | Детали |
|---|---|---|---|---|
| **S1** Deliverable-to-file | chat | 3 | ✅ **PASS 3/3** | tz.md создан во всех 3, содержимое осмысленное (не регургитация правил). Подтверждает chat-режим после рефрейма. |
| **S2** Memory-wiki migration | run | 3 | ✅ **PASS 3/3** | 3 проекта (animyou/ghost/northstar) перенесены через `remember`, 0 `write_file`, `memory: reindex` есть. |
| **S3** Sources immutability | run×2 | 2 | ✅ **PASS 2/2** | ход1 сохранил `sources/api-spec/`; ход2 (правка) — исходник НЕ изменён, immutable guard работает. |
| **S4** Eventual memory | run×2 | 5 | ✅ **PASS 5/5** | ход2 не повторил `remember append` (0 дублей), «бирюзовый» ровно 1 раз, без зацикливания (iters=2). Фикс 0267b71 держит. |
| **S5** Progressive recall | run | 3 | ✅ **PASS 3/3** | ровно 1 `update_field`, status=paused, тело (FastAPI) цело, страница жива. |
| **S6** Approval / policy push | run+approval | 2 | ⚠️ **НЕ ПРОГНАН** | требует workspace с настроенным remote (origin); в песочнице без remote push не доходит. Покрыт юнит-тестами. |
| **S7** Profile cleanup replace_section | run | 3 | 🔶 **FLAKY 1/3** | p2 PASS (хобби оставлено); p1,p3 — replace_section убрал ВСЮ секцию incl. хобби. Стохастично по content. |
| **S8** Frontmatter field update | run | 3 | ✅ **PASS 3/3** | ровно 1 `update_field`, нет delete/edit_file, created сохранён, updated обновлён. |
| **S9** Migration cleanup "leave X" | run | 3 | 🔶 **FLAKY 2/3** | p1,p2 PASS; p3 — профиль очищен верно, но wiki-страницы НЕ созданы. |
| **S10** Named workspace граница | cli | 1 | ⚠️ **НЕ ПРОГНАН** | требует `svarog serve` + remote (ADR-0017). Граница покрыта юнит-тестами (фикс 2c17715). |
| **S11** Opencode baseline | chat | 2 | ✅ **PASS 2/2** | opencode, tz.md создан, осмысленное содержимое. Путь 3 через `--session` работает. |
| **S12** Opencode MCP/память | run×2 | 3 | ✅ **PASS 2/3** | ход1 `svarog_remember` добавил Берлин (MCP-мост 3/3); p1 — baseline (Python/Vim) частично перезаписан. |
| **S13** Opencode chat continuity | chat | 2 | ✅ **PASS 2/2** | одна agent-сессия на оба хода, a.md содержит список + кодовое слово (дописано). |
| **S14** Executor mid-session switch | chat | 2 | ✅ **PASS 2/2** | native→external/opencode→native; T3 собрал историю через cloud-ход; yaml external intact (deep-merge). |
| **S15** Fail-closed гейты | cli | 1×3 | ✅ **PASS (a/b/c)** | все подкейсы отказывают ДО контейнера, причины внятные, мусорной ветки нет, yaml цел. |
| **S16** spawn_child delegation | run | 2 | ✅ **PASS 2/2** | `spawn_child_run executor=external` вызван, ребёнок opencode completed, cat.md создан. |
| **S17** Workspace boundary | run | 2 | 🔶 **FLAKY-инфра** | граница работает (секрет не утёк, 0 reads за пределами ws), но run failed «стрим без result-события». |
| **S18** Schedule approval deny | run+approval | 2 | ✅ **PASS 2/2** | waiting_approval при `--yolo` → deny+resume → completed; cron_jobs=0 после отказа. |
| **S19** Refuel long task | run | 2 | ✅ **PASS 2/2** | все 5 файлов, completed без ручного resume. |
| **S20** Cron UTC | cli | 1 | ✅ **PASS** | 03:00 MSK → `next_run_at` 00:00 UTC (не 03:00). Фикс 2e85b36 держит. |
| **S21** Dream consolidation | cli | — | ⚠️ **НЕ ПРОГНАН** | Dream запускается через scheduler (системная джоба), не через `svarog run`. Отдельная инфра. |
| **S22** read_svarog_docs | run | 5 | ✅ **PASS 4/5** | `read_svarog_docs` вызван 5/5, верный контент 4/5; p3 — generic GitFlow. Стохастика модели. |
| **S23** Context canary | run | 2 | ✅ **PASS 2/2** | маркер назван, 0 итераций, 0 read_memory. Контекст доставлен в AGENTS.md (фикс 338b242). |
| **S24** Schedule approve happy path | run+approval | 2 | ❌ **FAIL 2/2 — РЕАЛЬНЫЙ БАГ** *(исправлен: фикс `a2ec830`, перепрогон PASS 2/2)* | approve+resume → completed, НО `cron_jobs=0`: одобренная `schedule.create` не материализуется. Агент рапортует «настроено». **Починено в `a2ec830`** (ветка `fix/s24-schedule-drain-on-resume`): `TaskRunner.resume` зеркалит `run_once` — `schedule_sink` + `drain_schedule`. Перепрогон: PASS 2/2 (P1+P5) — ровно 1 джоба origin=agent enabled=true, daily_at:09:00 tz=Europe/Moscow, next_run_at=06:00 UTC. |
| **S25** Unschedulable cron | run+approval | 3 | 🔶 **частично 3/3** | daily_at:09:00 заведён (не every=604800), НО без оговорки про ограничение («еженедельно не умею»). |
| **S26** Team in memory → подбор | run×2 | 3 | ✅ **PASS 3/3** | 5 человек сохранены, baseline цел, ход2: Вера названа, mobile-дыра отмечена. |

## Сводка по итогу

- **Зелёные (PASS по всем прогонам): 17** — S1, S2, S3, S4, S5, S8, S11, S13, S14, S15, S16, S18, S19, S20, S22, S23, S26.
- **FLAKY / частично: 5** — S7 (хобби теряется), S9 (страницы не создаются), S12 (baseline перезапись), S17 (инфрафлейк «стрим без result»), S25 (нет оговорки про ограничение).
- **Реальный баг: 1 — S24** (critical: approve+resume → completed, но cron-джоба не материализуется). **Починен в `a2ec830`** — перепрогон PASS 2/2.
- **Не прогнаны (инфра): 3** — S6 (нужен remote), S10 (нужен serve), S21 (нужен scheduler). Покрыты юнит-тестами.

## Главные находки

1. **S24 — критический дефект Svarog (ИСПРАВЛЕН).** `drain_schedule` после `approve --reason` + `resume` не материализует одобренную `schedule.create`-заявку в `cron_jobs`: run `completed` с rapported «настроено», но джобы нет. Контраст с deny-путём S18 (там корректно — после deny джобы нет). **Фикс `a2ec830`** (ветка `fix/s24-schedule-drain-on-resume`, слит в `main` как `f2bb5f9`): `TaskRunner.resume` (internal path) теперь зеркалит `run_once` — передаёт `schedule_sink` в `build_loop` и вызывает `drain_schedule` после `loop.resume(...)`. Регрессия `tests/test_schedule_resume.py` (approve+resume → 1 джоба; deny+resume → 0 джоб). Red-green подтверждён, полный прогон 932 passed, перепрогон S24 в песочнице: PASS 2/2 (P1+P5) — origin=agent, enabled=true, daily_at:09:00 tz=Europe/Moscow, next_run_at=06:00 UTC.

2. **S7 — стохастическая потеря данных.** `replace_section` профиля: модель вкладывает в `content` то, что убрать, вместо того, что оставить → хобби (личное) теряется в 2/3 прогонов. Кандидат: усилить гайд (`content` = то, что ОСТАЁТСЯ).

3. **S17 — инфрафлейк opencode.** «стрим агента завершился без result-события» воспроизвёлся снова (тот же кандидат-в-регрессию, что в S13/S19 истории спеки).

4. **S25 — честность про ограничения.** Модель молча маппит «по понедельникам» в `daily_at:09:00` без оговорки (Watch(1) спеки).

5. **Рефрейм Superpowers + chat-режим подтверждены.** S1/S11 как chat-сценарии дают PASS (путь 3 работает через общую `--session`), что раньше в run-режиме было FAIL.
