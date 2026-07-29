# Группа cloud-executor (S11–S16, S22, S23), opencode

## S11 Opencode baseline (chat) — FLAKY (EOF chat)
2 хода chat, P1. Ход 1: brainstorming + writing-plans + уточняющий вопрос через
`svarog_ask_user` (ушёл в approval e122f3f9) — норма по преамбуле. Ход 2 (ответ
с уточнениями): агент сказал «сохраню в taskflow-tz.md», но файл НЕ создан —
chat завершился по EOF stdin до того, как агент завершил действие. Это
артефакт прогона (printf закрывает stdin), не отказ агента: путь 2 (approval)
требовал `approvals answer` + `resume`, а chat-режим держит сессию только пока
stdin открыт. S1 (та же задача) дал файл на ходе 1 — доказывает, что механика
рабочая. Вердикт FLAKY (инфра прогона), рекомендация: гонять chat через pty/expect.

## S12 Opencode MCP-мост (run×2) — PASS
ход1: `svarog_remember append` (MCP-мост) — факт «живу в Берлине» добавлен в
profile, baseline (Python/Vim) цел, reindex есть. ход2 (новый run): пересказал
все 3 факта, 0 итераций (контекст через managed AGENTS.md). curate чист.
Контраст с историей (раньше opencode врал на «запомни») — теперь честный
write+read end-to-end. Сценарий из «честности про отсутствие памяти» стал
зеркалом claude-code-пути, как в статусе спеки.

## S13 Chat continuity — PASS
2 хода chat (slow-feed, sleep между репликами). Ход 2 назвал кодовое слово
ЖАР-ПТИЦА без подсказки и ДОПИСАЛ его в a.md (bash echo >>, append, не
перезапись). a.md = alpha/beta/gamma + ЖАР-ПТИЦА. Continuity agent-сессии
доказана поведением (session id не печатается в --plain, но ход 2 помнит
контекст хода 1). Инфрафлейк «стрим без result» не воспроизвёлся.

## S15 Fail-closed гейты — PASS (a, b)
(a) `--supervised` + opencode → отказ ДО контейнера: «режим supervised с
внешним агентом требует enforcement='cooperative' и адаптера с hook-поддержкой
(tier 1)». (b) external + local-trusted → отказ: «external требует sandbox
docker». Оба с внятной причиной, без LLM/контейнера. (c) свап adapter на
отсутствующую секцию в chat — покрыт юнит-тестами (не run-команда), не гонял.
Мусорных веток не оставлено (гейт автономии до Flow C — фикс из истории держит).

## S16 Spawn_child delegation — PASS
docker sandbox + native родитель + external-секция opencode. Родитель делегировал
`executor=external`, ребёнок создал лимерик на ветке svarog/child-0c303be4,
родитель извлёк результат в child.md (на английском, «cat named Lou»). Оба файла
(parent.md + child.md) созданы. Замечание: 18 итераций — родитель тратил много
на git-object форензику (пытался читать .git/objects питоном), но цель достигнута.
Первый прогон с local-trusted ушёл в native-child (агент честно сказал «внешний
потребовал docker»); с docker-сетапом — external работает.

## S22 read_svarog_docs — FLAKY (0 вызовов тула)
Ответ ВЕРЕН по содержанию (Flow A память / Flow B скиллы / Flow C код — ADR-0003),
но `svarog_read_svarog_docs` НЕ вызван (0 итераций, ответ из претрейна). Это
Watch(1) S22 — модель со слабым agentic-index даёт generic ответ без тула. По
спеке Runs=5 (стохастика); 1 прогон = FLAKY. Ответ правильный, Assert (вызов
тула) не выполнен.

## S23 Контекст-канарейка — PASS
Маркер ЖЕЛЕЗНЫЙ-БАРСУК-7788 назван, 0 итераций, 0 read_memory — контекст
доставлен в окно через managed AGENTS.md, не поиском. Чистая механика.
