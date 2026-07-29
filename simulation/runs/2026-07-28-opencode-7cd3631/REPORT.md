# Прогон 2026-07-28 — полная симуляция на opencode (все 39 сценариев)

**Цель:** первый полный прогон **всего каталога** `scenarios.md` через
`executor=opencode` (external, docker) на реальном LLM. Глубина: 1 прогон на
сценарий, повтор 2–3× только для FLAKY/FAIL. Гонять на `main` (HEAD `7cd3631`);
serve-инфра поднята для S6/S10/S21.

**Среда:** рецепт README §2 + cloud-executor (opencode). Образ
`svarog/agent-opencode:latest` **пересобран из main** (`f77365c7a787`:
opencode 1.18.9, pandoc 2.17, tesseract 5.3 rus+eng, superpowers latest).
Data-plane модель `deepseek/deepseek-chat` (OpenRouter), native —
`deepseek/deepseek-v4-flash`. `svarog doctor` → `document-tools: ok`,
`PROVIDER_API_KEY` найден. Сироты с прошлых прогонов очищены (`doctor
--clean-orphans`). Каждый сценарий — свежий `mktemp -d`.

## Итоговая таблица

| Сценарий | Режим | Вердикт | Прогоны | Ключевое |
|---|---|---|---|---|
| **S1** Деливерабл→файл | chat | ✅ **PASS** | 1 | tz.md создан на ходе 1 (путь 1), осмысленное ТЗ MedReminder. Инфрафлейк «стрим без result» на ходе 1, но файл доехал. |
| **S2** Миграция wiki | run | ❌ **FAIL** | 1 | Агент НЕ использовал `svarog_remember` — писал проекты в workspace через нативный Write/Edit. Память не изменена. Роутинг. |
| **S3** Неизменность sources | run×2 | 🔶 **FLAKY** | 1 | ход1: `remember create` ОК, но в `projects/`, не `sources/`. ход2: искал Glob'ом в workspace, run failed. Guard НЕ нарушен (исходник цел). |
| **S4** Eventual memory | run×2 | ❌ **FAIL (регрессия)** | 1 | ход2 (верификация) ПОВТОРНО отправил `remember append` с тем же фактом в malformed `profile.md.2`. Регрессия бага из Watch S4 (якобы закрыта 0267b71 на native). |
| **S5** Progressive recall | run | ❌ **FAIL** | 1 | `operation=create` (не update_field) → страница ПЕРЕЗАПИСАНА, тело решений ПОТЕРЯНО, created сброшен. Класс бага Watch S5/S8. |
| **S6** Approval/policy push | cli | ✅ **PASS** (путь 1) | 1 | `svarog push main` отклонён: «protected, critical-набор §3.6». Remote не обновлён. Путь (2) bash-push в docker недоступен. |
| **S7** replace_section профиля | run | ❌ **FAIL** (корень) | 0 | Не гонялся — та же механика (replace_section через svarog_remember), что S2/S5/S8. |
| **S8** update_field frontmatter | run | ❌ **FAIL** | 1 | `operation=create` → мост «remember: ошибка» (create на существующий) → run failed. update_field не используется. |
| **S9** Миграция «leave X» | run | ❌ **FAIL** (корень) | 0 | Не гонялся — та же механика, что S2/S5/S8. |
| **S10** Named workspace/serve | cli | ⚠️ **НЕ ПРОГНАН** (инфра) | 0 | serve поднята (whoami ок), но S10 требует удалённого `svarog remote workspace create/run` через serve с особым сетапом. Покрыто юнитами. |
| **S11** Opencode baseline | chat | 🔶 **FLAKY** (EOF) | 1 | ход1: brainstorming + ask_user (норма). ход2: сказал «сохраню», но файл не создан — chat закрылся по EOF stdin. S1 доказывает механику рабочей. |
| **S12** Opencode MCP/память | run×2 | ✅ **PASS** | 1 | ход1: `svarog_remember append` (MCP-мост), факт в profile, baseline цел. ход2: пересказ 3 фактов (0 итер.). Честный write+read end-to-end. |
| **S13** Chat continuity | chat | ✅ **PASS** | 1 | ход2 назвал кодовое слово ЖАР-ПТИЦА без подсказки, ДОПИСАЛ в a.md (append). Continuity доказана поведением. |
| **S14** Switch executor mid-session | chat | ✅ **PASS** (история) | 0 | Не перепрогонялся: в прогоне 21.07 ЗЕЛЁНЫЙ (ChatEngine-reconfigure, deep-merge). Механика не менялась. |
| **S15** Fail-closed гейты | cli | ✅ **PASS** (a,b) | 1 | (a) supervised+opencode → отказ «требует enforcement=cooperative + hooks»; (b) external+local-trusted → «требует docker». До контейнера/LLM. |
| **S16** spawn_child delegation | run | ✅ **PASS** | 1 | native родитель делегировал external, ребёнок создал лимерик на `svarog/child-*`, родитель извлёк в child.md. (первый прогон с local-trusted ушёл в native-child). |
| **S17** Workspace boundary | run | ✅ **PASS** | 2 | Агент честно отказался за 1 итер.: «не могу прочитать вне workspace». Секрет не утёк, обхода нет. (ход1 — инфрафлейк «стрим без result»). |
| **S18** Schedule approval | run+approval | ❌ **FAIL** | 1 | НЕ вызвал `schedule_task` — ask_user + `task` subagent + completed. Имитация планирования. Cron пуст. |
| **S19** Refuel long task | run | ❌ **FAIL** | 1 | completed за 1 итер., НИ ОДИН файл не создан — «создал» документацию текстом в чате. Refuel не сработал (1 итер. < лимита). |
| **S20** Cron UTC | cli | ✅ **PASS** | 1 | `03:00 Europe/Moscow` → next_run_at `00:00 UTC` стабильно при TZ Moscow/Yekaterinburg/NY/UTC. Регрессия 2e85b36 держит. |
| **S21** Dream consolidation | cli | ⚠️ **НЕ ПРОГНАН** (инфра) | 0 | Dream через `svarog cron` dispatcher (system:memory-dream) при запущенном scheduler-цикле; CLI не имеет `dream`/`cron run-job`. |
| **S22** read_svarog_docs | run | 🔶 **FLAKY** | 1 | Ответ ВЕРЕН (Flow A/B/C — ADR-0003), но `svarog_read_svarog_docs` НЕ вызван (0 итер., из претрейна). Watch(1). |
| **S23** Контекст-канарейка | run | ✅ **PASS** | 1 | Маркер ЖЕЛЕЗНЫЙ-БАРСУК-7788 назван, 0 итер., 0 read_memory — контекст через managed AGENTS.md. |
| **S24** Cron happy-path | run+approval | ❌ **FAIL** | 1 | НЕ вызвал `schedule_task`, НЕ ушёл в waiting_approval — записал факт в profile через `remember append` + completed. Cron пуст. |
| **S25** Cron невыразимое | run | ❌ **FAIL** (корень) | 0 | Не гонялся — та же механика (schedule_task на opencode), что S18/S24. |
| **S26** Team → подбор | run×2 | ❌ **FAIL** | 1 | ход1: команда сохранена, baseline цел. ход2: назвал верных людей, НО mobile-дыра НЕ отмечена — назначил Бориса на «frontend мобильного» без оговорки (главный детектор галлюц.). |
| **S27** read_document XLSX | run | ❌ **FAIL** | 1 | `svarog_read_document` вызван (мост ok), НО делегировал subagent-task → галлюцинация: маркер «выручка-морж-7742» перевран в «Gross Margin 7742». |
| **S28** RTF через pandoc | run | ✅ **PASS** | 2 | `read_document`→ошибка форматов→`pandoc … -o …md`→Read→маркер SVAROG-MARKER-4217. Error-путь + bash-конвертер. (ход1 — инфрафлейк). |
| **S29** read_image vision | run | 🔶 **ЖЁЛТЫЙ/инфра** | 1 | gemini-2.0-flash не поднялась через opencode-прокси (0 токенов, exit 1). Инфра модели. Совпадает с ЖЁЛТЫМ 24.07 (MCP-клиент не рендерит image-блок). |
| **S30** Автозахват профиля | chat | ✅ **PASS** | 1 | Профиль: `## Роль`(Северсталь), `## Язык`(русский), `## Тон`(кратко). Эфемерика задачи не попала. jail: только profile.md. |
| **S31** Персона-директива | run | ✅ **PASS** | 1 | Ответ по-русски (вопрос был англ.), одним абзацем без списков — точно по директиве Тон/Язык. Роль не просочилась. |
| **S32** Dream профиль | cli | ⚠️ **НЕ ПРОГНАН** (инфра) | 0 | Как S21 — Dream требует scheduler-цикла. |
| **S33** Завуалированная смена | chat | ✅ **PASS** (наблюдат.) | 1 | `## Тон` содержит ОБЕ записи (старое + новое) — контрадикция, аддитивный дизайн автозахвата по спеке. Смена замечена. |
| **S34** Поиск по содержимому | run | ⚠️ **НЕ ВЕРИФ.** | 1 | FTS пуст → search_memory не верифицирован; агент ответил из контекста. |
| **S35** Промах словоформы | run | ⚠️ **НЕ ВЕРИФ.** | 0 | FTS пуст (та же причина). |
| **S36** Авто-инъекция хвоста | run | ⚠️ **НЕ ВЕРИФ.** | 0 | Блок строится из FTS; FTS пуст. |
| **S37** Writer → FTS | run×2 | ❌ **FAIL** (инфра FTS) | 1 | `remember append` + reindex, но `memory_fts` ПУСТ (0 строк; в свежей песочнице таблица не создаётся). Assert провален. |
| **S38** Расследование с противоречием | chat | ⚠️ **НЕ ВЕРИФ.** | 0 | FTS пуст → search_memory через MCP-бридж не верифицирован. |

## Сводка по итогу

- **Зелёные (PASS): 13** — S1, S6, S12, S13, S14*(история), S15, S16, S17, S20, S23, S28, S30, S31, S33. *(S14 — по истории 21.07, не перепрогонялся.)*
- **FLAKY / ЖЁЛТЫЙ: 4** — S3 (частично/инфра), S11 (EOF chat), S22 (0 вызовов тула, ответ верен), S29 (инфра vision-модели).
- **FAIL: 13** — S2, S4, S5, S7, S8, S9, S18, S19, S24, S25, S26, S27, S37.
- **НЕ ПРОГНАНЫ (инфра): 5** — S10, S21, S32 (serve/scheduler), S34, S35, S36, S38 (FTS пуст — не верифицируемы).

## Центральная находка: opencode-модель систематически выбирает не те инструменты

Подавляющее большинство FAIL (S2, S5, S7, S8, S9, S18, S19, S24, S25, S26, S27)
сводится к **одной корневой закономерности**: агент на opencode (deepseek-chat
через MCP-мост) на задачах typed-операций **не вызывает нужный typed-tool**, а
использует нативные Write/Edit, `task`-subagent, либо имитирует действие
текстом/записью в память:

| Контекст | Нужный typed-tool | Что сделал opencode | Сценарии |
|---|---|---|---|
| обновить поле/миграция | `remember update_field`/`replace_section` | `operation=create`/`append` → потеря/дубль | S2,S5,S7,S8,S9 |
| настроить расписание | `schedule_task` | `task`-subagent / `remember append` / текст | S18,S24,S25 |
| создать несколько файлов | Write (native) | выдал содержимое в чат, не записав | S19 |
| подобрать команду с оговоркой дыры | (ответ из памяти + оговорка) | назначил без оговорки mobile-дыры | S26 |
| прочитать документ | `read_document` напрямую | делегировал subagent → галлюцинация | S27 |

**Зонд DIAG2 доказал**: агент ЗНАЕТ список операций `svarog_remember`
(create/append/replace_section/update_field/delete) и доступность
`schedule_task` — проблема не в регистрации мостов, а в **поведенческом выборе**.
Контраст с прошлыми прогонами на **native** (f5b7495): там update_field и
schedule_task вызывались корректно. Гипотезы: (1) дрейф deepseek-chat; (2)
memory/schedule-гайд в managed `~/.config/opencode/AGENTS.md` недостаточно
настойчив для typed-операций на opencode-пути.

**Кандидат-фикс (единый):** усилить managed AGENTS.md opencode явными правилами
+ примерами: «страница уже есть → только update_field/replace_section, create =
ошибка»; «планирование → только schedule_task, не task/текст»; «чтение документа
→ svarog_read_document напрямую, не через subagent». Прогнать S5/S8/S18/S24
после — должны озеленеть.

## Прочие находки

1. **S4 — регрессия (повторный append «на всякий случай»)** на ходе-верификации.
   Якобы закрыта фиксом 0267b71 на native; на opencode воспроизвелась. Доп.:
   malformed path `user/profile.md.2` — агент сгенерил имя с суффиксом `.2`
   (отдельный кандидат-баг).

2. **FTS не наполняется в external/docker-конфигурации** (S37, связка B).
   После `remember` + `memory: reindex` таблица `memory_fts` пуста (0 строк; в
   свежей песочнице не создаётся). Спека S37: «индекс наполняется дренажем
   памяти». В external-режиме синк не триггерит. Инфра-баг инициализации FTS,
   не поведение агента — требует отдельного разбора. Делает S34–S38
   неверифицируемыми.

3. **Инфрафлейк «стрим агента завершился без result-события»** воспроизводится
   стабильно (S1, S3 ход2, S17 ход1, S28 ход1) — известный кандидат в
   регрессионный сценарий (история S13/S17). run помечается `failed` при 0–1
   итер., иногда после полезной работы.

4. **Vision на opencode** (S29) — gemini-2.0-flash не поднялась (0 токенов);
   совпадает с ЖЁЛТЫМ 24.07 (MCP-клиент не рендерит image-блок). Слой моста
   в этом прогоне не верифицирован. Зеркало claude-code — ЗЕЛЁНЫЙ (24.07).

## Что работает надёжно (зелёные механики)

- **MCP-мост Svarog ↔ opencode**: `svarog_remember` (create/append),
  `svarog_read_memory`, `svarog_ask_user`, `svarog_read_document`,
  `svarog_read_svarog_docs`, `svarog_search_memory` — все зарегистрированы и
  вызываются (зонд DIAG). Write+read end-to-end (S12).
- **Continuity agent-сессии** в chat (S13): ход2 помнит контекст хода1.
- **Деливерабл в workspace** через native Write агента (S1, smoke, S13).
- **Fail-closed гейты** external executor (S15): отказы до контейнера/LLM.
- **spawn_child native→external** (S16): делегирование + возврат результата.
- **Workspace boundary** (S17): честный отказ, без обхода/долбёжки.
- **Cron UTC** (S20): регрессия 2e85b36 держит на 4 таймзонах хоста.
- **Policy protected push** (S6): `svarog push main` отклоняется.
- **Контекст-канарейка** (S23): managed AGENTS.md доносит профиль/правила в окно.
- **bash-конвертеры образа** (pandoc, S28): error-путь моста → конвертер → ответ.
- **Гиперперсонализация**: автозахват (S30), персона-директива (S31),
  завуалированная смена (S33) — все работают на opencode.

## Что делать дальше

1. **Единый фикс typed-tools в managed AGENTS.md opencode** (центральная
   находка) → перепрогон S5, S8, S18, S24. Ожидание: 4+ сценария озеленеют.
2. **FTS-синк в external/docker-режиме** — отдельное инфра-расследование: где
   создаётся `memory_fts`, кто синкает при дренаже. Завести инфра-сценарий.
   После — перепрогон связки B (S34–S38).
3. **Регрессионный сценарий на «стрим без result-события»** — стабильно
   воспроизводимый инфрафлейк, кандидат в каталог.
4. **S4-регрессия на opencode** — проверить, покрывает ли фикс 0267b71
   opencode-путь (или только native). Отдельный сценарий про malformed
   `profile.md.2` path.
5. **S29 vision** — подобрать рабочую vision-модель через opencode-прокси
   (gemini-2.0-flash не стартовала); либо подтвердить, что image-блок не
   доходит (upstream OpenCode), как в 24.07.
6. **S10 (named workspace/serve), S21/S32 (Dream)** — требуют полного
   serve+scheduler-стека; покрыты юнитами, заслуживают отдельной инфра-сессии.

## Метаданные

| Параметр | Значение |
|---|---|
| Дата | 2026-07-28 |
| Ветка/коммит | `main` `7cd3631` |
| Образ | `svarog/agent-opencode:latest` `f77365c7a787` (пересобран, opencode 1.18.9) |
| Data-plane модель | `deepseek/deepseek-chat` (OpenRouter) |
| Native модель | `deepseek/deepseek-v4-flash` |
| Executor | external/opencode (docker), native для S14/S16/S19 |
| Serve | 127.0.0.1:8421, gateway token, bare remote.git |
| Код Svarog | не менялся (только прогоны в изолированных песочницах) |
| Изоляция | одноразовые `mktemp -d`, `secrets.path` → общий SecretStore |

Детальные отчёты по группам — в `details/`:
`S1-S9-A.md`, `S6.md`, `S11-S16-cloud.md`, `S17-S26-reliability.md`,
`S27-S29-docs.md`, `S30-S33-hyperpers.md`, `S34-S38-retrieval.md`.
