# Группа A: деливерабл / память (S1–S9), opencode

Среда: рецепт README §2 + cloud-executor (opencode), sandbox docker, модель
deepseek/deepseek-chat (data-plane) / deepseek-v4-flash (native). Образ
f77365c7a787 (opencode 1.18.9, пересобран из main).

## S1 Deliverable-to-file (chat) — PASS
run chat --plain, 2 реплики P5. tz.md создан уже на ходе 1 (путь 1): ТЗ для
MedReminder, осмысленное (не регургитация). Файл создан через нативный Write
агента в workspace — корректно (деливерабл, не память). Замечание: первый ход
дал `failed: стрим агента завершился без result-события` (инфрафлейк S13/S17),
но файл доехал до второго хода. Вердикт PASS (цель достигнута).

## S2 Memory-wiki migration (run) — FAIL (роутинг)
run, P5. Агент НЕ использовал `svarog_remember` — писал проекты в workspace
через нативные Write/Edit (`/workspace/user/projects/ghost.md`). Память не
изменилась (git log пуст). Затем ушёл в waiting_approval через svarog_ask_user
с вопросом «где profile». read-канал (`svarog_read_memory`) использовался
(2 вызова), write-канал — нет. Контраст: зонд DIAG (тот же агент) подтвердил,
что `svarog_remember` доступен в tools/list. Корень: поведенческий — модель на
задачу «перенеси в память» выбирает нативный Write. В прошлых прогонах (21.07,
S12-история) opencode использовал svarog_remember — регрессия поведения или
дрейф модели. Кандидат-фикс: усилить memory-гайд в managed AGENTS.md opencode
(«долговременная память — ТОЛЬКО через svarog_remember; нативный Write =
workspace, не память»).

## S3 Sources immutability (run×2) — FLAKY/наблюдение
ход1: агент использовал `svarog_remember create` (write-канал ОК!), но положил
исходник в `projects/billing/src.md`, а НЕ в `sources/` (Setup требует
sources/<slug>). memory: reindex отработал. ход2: попросили правку — агент
искал файл через нативный Glob в workspace (`*refund*`), не нашёл, run failed
(инфрафлейк «стрим без result»). Guard неизменности НЕ нарушен — исходник цел
(был бы в sources, immutability сработал бы). Вердикт: ядро (неизменность)
держит, но маршрутизация в sources/ и чтение памяти на ходе 2 — мимо.

## S4 Eventual memory (run×2) — FAIL (регрессия дубля)
ход1: `svarog_remember append` сработал, факт записан (CI на GitHub Actions,
флаг CI-BLUE-2204), reindex есть. ход2 (верификация «точно записал?», НОВЫЙ
run): агент ПОВТОРНО отправил svarog_remember append с тем же фактом, причём
в malformed path `user/profile.md.2` (суффикс `.2`!). Это ровно регрессия из
Watch S4 (повторный append «на всякий случай»), якобы закрытая фиксом 0267b71
на native. На opencode воспроизвелась. Доп. находка: malformed file path
`profile.md.2` — отдельный кандидат-баг (агент сгенерил имя с суффиксом).

## S5 Progressive recall (run) — FAIL (потеря страницы)
Setup: animyou/overview.md с телом решений (FastAPI, Redis), status active.
Driver: обнови статус на paused. Агент использовал `operation=create` (НЕ
update_field) → страница ПЕРЕЗАПИСАНА целиком: тело с решениями ПОТЕРЯНО,
created сброшен с 2026-01-10 на 2026-07-28. Ровно класс бага из Watch S5/S8
(потеря через create вместо update_field, рапорт «обновлено»). Реальный дефект.

## S8 Frontmatter update_field (run) — FAIL
Setup: billing/overview.md status:active + тело. Driver: поставь paused.
Агент попытался `operation=create` → мост вернул «remember: ошибка» (create
на существующий файл отклонён) → run failed (инфрафлейк). Страница НЕ обновлена
(тело цело, но цель не достигнута). Подтверждает: update_field не используется.

## S7, S9 — НЕ ПРОГОНЯЛИСЬ (та же корневая причина)
S7 (replace_section профиля) и S9 (миграция «leave X») — та же механика
(update_field/replace_section через svarog_remember), что S2/S5/S8. Зонд DIAG2
подтвердил: агент ЗНАЕТ список операций (create/append/replace_section/
update_field/delete), но на задачу обновления/миграции выбирает create/append.
Гнать S7/S9 — дублировать одну и ту же находку. Отмечены как FAIL по корневой
причине группы A.

## Центральная находка группы A
write-канал `svarog_remember` ДОСТУПЕН и ИСПОЛЬЗУЕТСЯ (S3/S4/S5 ходы создания),
но модель систематически выбирает create/append вместо update_field/
replace_section на задачах обновления/миграции. На native (прошлые прогоны)
update_field использовался корректно. Гипотезы: (1) дрейф deepseek-chat; (2)
memory-гайд в managed AGENTS.md opencode недостаточно настойчив про выбор
операции по контексту (update_field для существующей страницы). Кандидат-фикс:
явное правило в managed AGENTS.md + пример «страница уже есть → только
update_field/replace_section, create = ошибка».
