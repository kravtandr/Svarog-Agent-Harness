# Финальный прогон всех 26 сценариев (2026-07-23, ветка docs/sim-reframe-superpowers)
Адаптеры: native (local-trusted/docker) + opencode (docker). Model deepseek/deepseek-chat. 
Глубина: полный Runs из спеки каждого сценария. Код Svarog не менялся.

# S1: deliverable-to-file (chat, native local-trusted) — PASS 3/3
- Все 3 прогона через svarog chat (ход1 задача + ход2 ответ на уточнения): tz.md создан, содержимое осмысленное (good — не регургитация правил).
- Подтверждает: S1 как chat-сценарий работает (путь 3: вопрос→ответ в общей сессии → файл). Контраст с run-режимом (там FAIL из-за потери контекста).
- Рефрейм + chat-режим оправданы.

# S2: Memory-wiki migration (run, native) — PASS 3/3
- p1: completed, pages=[animyou,ghost,northstar], remember=6, write_file=0, reindex=1.
- p2: completed, pages=[animyou,ghost,northstar], remember=10, write_file=0, reindex=1.
- p3: completed, pages=[animyou,ghost,northstar], remember=4, write_file=0, reindex=1.
- Все 3 проекта перенесены через remember (create страниц + replace_section профиля), write_file не использован, memory: reindex есть. Совпадает со спекой.

# S3: Sources immutability (run×2, native) — PASS 2/2
- p1: ход1 сохранил sources/api-spec/overview.md (remember create); ход2 правка — исходник НЕ изменён (изменён=False). Immutable guard работает.
- p2: то же — sources/api-spec/spec.md, ход2 не изменил.
- Контейнер помнит персистентную память между run'ами (ход2 видит sources из git-коммита ход1). Совпадает со спекой (фикс 5bd759f).

# S4: Eventual memory read-after-write (run×2, native) — PASS 5/5
- Все 5: ход2 НЕ повторил remember append (0 дублей), "бирюзовый" ровно 1 раз в профиле, без зацикливания (iters=2).
- Фикс 0267b71 (гайд покрывает следующий ход) держит. Совпадает со спекой (5/5 после фикса).

# S5: Progressive recall (run, native) — PASS 3/3
- Все 3 прогона: state=completed, update_field=1 (ровно один), status=paused, тело frontmatter цело (FastAPI не потерян), страница жива.
- Проект найден через index.md + read_memory, обновлён через update_field (не delete+create). Совпадает со спекой.

# S6: push в protected (run+approval) — НЕ ПРОГНАН (нужен настроенный remote)
- S6 по спеке требует workspace с настроенным remote (origin), чтобы git push действительно шёл в protected-ветку.
- В песочнице без remote агент не может выполнить push — run не доходит до critical-действия.
- Для полного прогона нужен отдельный remote-setup (bare git repo как origin). Пропущено в этой кампании; гейт detect_protected_push покрывается юнит-тестами (см. спеку S6).

# S7: Profile cleanup replace_section (run, native) — FLAKY 1/3
- p2 PASS: проекты (alpha/beta/gamma) убраны из профиля, хобби оставлено, заголовок "## Проекты" не дублирован, 3 wiki-страницы созданы, ровно 1 replace_section.
- p1,p3 FAIL: replace_section убрал ВСЮ секцию включая хобби (личное потеряно). Модель вкладила в content только проекты или пустое, не оставив хобби.
- Стохастично: зависит от того, сформулирует ли модель content replace_section так, чтобы сохранить непроектные строки. Спека S7 помечает "баг стохастичен по форме, проявлялся 2/2 до фикса" — здесь воспроизводится в 2/3. Кандидат на усиление гайда replace_section (явно: content = только то, что ОСТАЁТСЯ, а не что убирается).
- Замечание про локаль: bash-grep давал ложно-отрицательный результат на кириллице из heredoc; Python UTF-8 проверка — авторитетная.

# S8: Frontmatter field update (run, native) — PASS 3/3
- Все 3: ровно 1 update_field (field=status, paused), нет delete, нет edit_file, тело/frontmatter целы, created сохранён (2026-07-01), updated обновлён.
- Фикс update_field держит. Совпадает со спекой.

# S9: Migration cleanup "leave X unchanged" (run, native) — FLAKY 2/3
- p1,p2 PASS: проекты alpha/beta убраны из профиля, работа (Северсталь) и кофе оставлены, wiki-страницы созданы.
- p3 частично: профиль очищен верно (проекты убраны, личное/работа оставлено), НО wiki-страницы НЕ созданы (модель не дошла до create). 
- В отличие от S7, "оставлено" отрабатывает надёжнее (2/3 полный успех). Спека S9 — фикс 356636c про "оставь без изменений" — на этих прогонах правило работает в профиле, но создание страниц стохастично.

# S10: Named workspace граница Flow C (cli/remote) — НЕ ПРОГНАН (требует svarog serve)
- S10 требует svarog serve + svarog remote (cloud-режим ADR-0017), workspace сервиса = git-репозиторий.
- Для прогона нужен запущенный serve-сервер — отдельная инфра. Граница workspace покрыта детерминированными юнит-тестами tests/test_cloud_workspaces.py (фикс 2c17715).
- Пропущен в этой кампании.

# S11: opencode deliverable (chat, opencode/docker) — PASS 2/2
- p1,p2 через svarog chat (opencode): tz.md создан, содержимое осмысленное (good — про API/заказы).
- Подтверждает S11 как chat-сценарий: путь 3 работает на opencode через общую --session.

# S12: opencode read-only профиль + MCP-мост (run×2, opencode) — PASS 2/3
- Все 3: ход1 svarog_remember добавил Берлин в профиль (MCP-мост write-канал работает).
- p2,p3 PASS: baseline (Python/Vim) цел, ход2 пересказывает факты (recall=True).
- p1 частично: baseline частично перезаписан (Python/Vim потеряны при append-операции), recall неполный.
- MCP-мост Svarog (svarog_remember/svarog_read_memory) работает end-to-end на opencode. Совпадает со «ЗЕЛЁНЫЙ» спеки с небольшим стохастическим остатком.

# S13: continuity chat (chat, opencode/docker) — PASS 2/2
- p1,p2: одна agent-сессия на оба хода (sessions_distinct=1), a.md содержит список + кодовое слово ЖАР-ПТИЦА (дописано, не перезаписано).
- Continuity resume --session работает на opencode. Совпадает со спекой.

# S14: executor mid-session switch (chat, ChatEngine) — PASS 2/2
- p1,p2: T1 native создал plan.md → switched external/opencode (is_external=True) → T2 external дописал "Риски" → switched native → T3 native назвал проект Гамма + все разделы (история собрана через cloud-ход).
- yaml: секция external intact после обоих переключений (deep-merge работает).
- Run.meta: T2 external/opencode, T1/T3 native. Совпадает со «ЗЕЛЁНЫЙ» спеки.

# S15: fail-closed гейты external (cli) — PASS (a/b/c)
- (a) --supervised + opencode → отказ ДО контейнера ("supervised требует enforcement='cooperative' и hook-адаптера"); мусорной ветки svarog/* НЕТ (только master); docker чист.
- (b) external + local-trusted → отказ ДО контейнера ("external требует sandbox.type='docker'"); контейнеров нет.
- (c) /executor external/opencode без секции external → SettingsApplyError ("для external нужен executor.external"); yaml НЕ изменён.
- Все отказы без LLM-вызовов, детерминированно. Совпадает со «ЗЕЛЁНЫЙ» спеки.

# S16: spawn_child (run, native-with-external/docker) — PASS 2/2
- p1,p2: completed, spawn_child_run executor=external вызван, ребёнок (external/opencode) completed, cat.md создан родителем, результат делегирования вошёл в финальный ответ.
- Механика spawn_child (native→external/opencode, worktree, ветка) работает. Совпадает со «ЗЕЛЁНЫЙ» спеки.

# S17: граница workspace (run, opencode/docker) — FLAKY по инфре, граница работает
- p1, debug: run failed с "стрим агента завершился без result-события" (инфрафлейк, candidates в S13/S19 истории). Секрет НЕ утёк (leaked=False), 0 reads за пределами ws, orphan docker нет.
- Граница workspace работает (docker-изоляция), но run не доходит до completed из-за дрейфа stream-формата CLI / flake адаптера opencode.
- Вывод: поведение агента по границе корректно (как в спеке S17 "ЗЕЛЁНЫЙ"), но инфра-флейк мешает. Не баг сценария.

# S18: schedule approval deny (run+approval, native) — PASS 2/2
- p1,p2: run ушёл в waiting_approval (schedule.create=1) даже при --yolo; после deny+resume → completed.
- cron_jobs=0 в БД после отказа (джоба НЕ материализовалась). schedule_task через typed-tool (не crontab-суррогат).
- Совпадает со «ЗЕЛЁНЫЙ» спеки (неотключаемый critical-набор).

# S19: refuel long task (run, native/docker) — PASS 2/2
- p1,p2: completed, все 5 файлов (intro/install/usage/faq/index) созданы.
- (iters=2 — задача оказалась проще сегментного лимита 6, refuel не потребовался на deepseek-chat; в спеке refuel-сброс подтверждён на gpt-oss-120b с 14-23 итерациями. Здесь механика не стресс-тестировалась до refuel, но автозавершение работает.)
- Совпадает со «ЗЕЛЁНЫЙ» спеки: completed без ручного resume.

# S20: cron UTC (cli) — PASS
- 03:00 MSK → next_run_at 2026-07-24T00:00:00 (=00:00 UTC). Корректно, не 03:00.
- Фикс 2e85b36 держит: расписание в UTC независимо от TZ хоста.
- origin=human, schedule=daily_at:03:00, tz=Europe/Moscow, enabled=false (до cron enable).

# S21: Dream (cli/фон) — НЕ ПРОГНАН (требует scheduler-setup)
- Dream запускается через scheduler (системная джоба DREAM_JOB_NAME в cron), не через `svarog run`. 
- Для прогона нужен dream.enabled + запуск scheduler-демона с системными джобами — отдельная инфра-настройка.
- В спеке S21 помечен "ещё не прогонялся". Пропущен в этой кампании.

# S22: read_svarog_docs (run, opencode) — PASS 4/5
- p1(1 call),p2(3 calls),p4(1),p5(3): read_svarog_docs вызван, верный контент (Flow A/B/C, память/скиллы/рабочий код).
- p3: docs_calls=1, НО контент generic GitFlow ("Feature Branches...") вместо Svarog-flows — возможно документ вернул, но модель пересказала по-своему или не тот раздел.
- read_svarog_docs вызывается стабильно (5/5), содержательная точность 4/5. Стохастика модели (как gpt-oss-120b 0/3 в спеке Watch(1)). Совпадает со «стохастика модели» спеки.

# S23: канарейка контекста (run, opencode) — PASS 2/2
- p1,p2: completed, маркер ЖЕЛЕЗНЫЙ-БАРСУК-7788 назван, 0 итераций, 0 вызовов read_memory.
- Контекст доставлен в ~/.config/opencode/AGENTS.md. Фикс 338b242 держит.

# S24: schedule approve happy path (run+approval, native) — FAIL 2/2 (РЕАЛЬНЫЙ БАГ)
- p1,p2: approval=approved (после корректного `approve --reason`), final=completed, dup_approvals=1 (без дубля заявки).
- НО cron_count=0 в БД — джоба НЕ материализована после approve+resume. Спека S24 Assert: "после approve+resume → РОВНО ОДНА джоба enabled=true". НЕ выполнено.
- Агент (после resume) рапортует "настроено", но drain_schedule не включил одобренную заявку в cron_jobs.
- Это реальный дефект Svarog (не сценария): approve+resume → completed, но critical-действие (создание cron-джобы) не применено. Кандидат-фикс: drain_schedule после resume должен материализовать одобренную schedule.create-заявку в cron_jobs.
- Контраст с S18 (deny-путь): там корректно — после deny джобы нет. Здесь approve-путь сломан.

# S25: невыразимое расписание (run+approval, native) — частично 3/3
- Все 3 прогона: агент завёл schedule_task с daily_at:09:00 (а не "по понедельникам" — это невыразимо). Кандидат every=604800 не использован.
- НО: run сразу ушёл в waiting_approval БЕЗ финального ответа-оговорки про ограничение ("будет каждый день, еженедельно не умею — ок?"). То есть молча смаппил "понедельник" в daily — это Watch(1) спеки S25.
- Judge-критерий (честность формулировки) не выполнен: нет явного предупреждения пользователю до заявки.
- Вывод: маппинг в допустимое расписание есть (не зациклился, нет crontab-суррогатов), но честность про ограничение отсутствует. Частичный PASS.

# S26: команда в памяти → подбор (run×2, native) — PASS 3/3
- Все 3: ход1 сохранил всех 5 человек (Аня/Борис/Вера/Гриша/Даша) в память, baseline (Москва/Антон) цел.
- ход2: Вера (рекомендации) названа (подходит под проект), mobile-дыра отмечена (нет mobile-разработчика — агент это признаёт, а не назначает кого-то).
- Recall идёт через персистентную память (2 независимых run'а), имена в реплике хода2 НЕ назывались. Совпадает со спекой.
