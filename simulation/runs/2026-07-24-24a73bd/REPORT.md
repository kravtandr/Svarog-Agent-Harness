# Прогон 2026-07-24 — документы и изображения (S27–S29, opencode)

**Цель:** проверка фичи «поддержка документов и изображений» (spec 2026-07-24,
мердж 5e0307e) на внешнем executor'е **opencode**.
**Среда:** `/tmp/svarog-sim-docs-*` по рецепту README §2 + cloud-executor;
образ `svarog/agent-opencode:latest` пересобран после мерджа (pandoc 2.17,
pdftotext 22.12, tesseract 5.3 rus+eng подтверждены в контейнере); модели —
`deepseek/deepseek-chat` (S27/S28), `google/gemini-3.5-flash` (S29, vision);
`svarog doctor` → `document-tools: ok`.

| Сценарий | Статус | Прогоны | Ключевые run'ы |
|---|---|---|---|
| S27 read_document (XLSX) | **ЗЕЛЁНЫЙ** | 2/2 PASS | c27386e3, 7195f316 |
| S28 bash-конвертер (RTF) | **ЗЕЛЁНЫЙ после фикса 24a73bd** | 2/2 пост-фикс | 014c949a (FAIL до фикса), c31da3af, 08205af9 |
| S29 read_image (vision) | **ЖЁЛТЫЙ** | 2 (см. ниже) | f6e4a488, 2425f171 |

## S27 — read_document через мост

Оба прогона (точная и небрежная формулировки): единственный вызов
`svarog_read_document path=data/report.xlsx` → Markdown-таблица с маркером
`выручка-морж-7742` в tool_result и финальном ответе, 1 итерация.
`Run.meta`: `executor=external`, `adapter=opencode`, `agent_session_id=ses_…`.
Hint из AGENTS.md доводит модель до инструмента без блужданий.

## S28 — RTF: error-путь моста + pandoc образа

**Найден и исправлен баг эргономики** (главный результат прогона): агент в
sandbox передаёт мосту пути своими глазами — контейнерные абсолютные
(`/workspace/legacy.md`, `/tmp/opencode/…`), а мост резолвил их на хосте и
fail-closed отвергал «вне workspace». Прогон 014c949a: 4 отвергнутых вызова
подряд, агент зациклился в конвертациях и упал инфрафлейком «стрим без
result-события» (известен по S13). Фикс **24a73bd**: `/workspace/…`
нормализуется в относительный путь (unit-регрессии в
`tests/test_document_tools.py`).

Пост-фикс 2/2 идеальная траектория: `read_document(rtf)` → ошибка со списком
форматов и подсказкой про pandoc (проверяемый error-путь) → `pandoc … -o
/tmp/opencode/legacy.md` (слой 1 работает) → нативный Read → ответ с
`SVAROG-MARKER-4217` и всеми фактами.

Наблюдение вне скоупа фичи: один раз агент спросил `svarog_ask_user`
«каким инструментом конвертировать» и, не дождавшись ответа, завершил run
`completed` — `approvals answer` + `resume` уже невозможны, вопрос повис
(run a010b1bb). Кандидат в отдельный сценарий: ask_user в yolo-run без
человека должен либо suspend'ить, либо не оставлять висячий approval.

## S29 — read_image: мост работает, vision через OpenCode не подтверждён

Мост 2/2: `svarog_read_image` succeeded, image content block отдан
(«read_image: ok»). Но модель, судя по траекториям, картинку **не видела**
(Watch 1 сценария):

- Прогон f6e4a488: после «ok» — 5 ходов программной криминалистики (PIL
  отсутствует, `file`, node fs, финально node+zlib-декодер PNG); ответ
  «красный квадрат #FF0000» верен, но добыт из байтов, не из vision.
- Прогон 2425f171: после read_image только Glob и завершение **без
  финального текста** (в trace одни step_start) — флейк финала на
  gemini-3.5-flash (text/reasoning-события не пришли).

Вывод: слой моста (blocks в `tools/call`) исправен; узкое место — MCP-клиент
OpenCode 1.18.4, который, по-видимому, не рендерит image-блоки в контент
модели. Кандидаты: зеркальный прогон на claude-code; issue upstream OpenCode;
до тех пор vision-путь на opencode считать нерабочим (bash-путь агент находит
сам).

## Инфраструктурные заметки прогона

- Дефолтный `skills.paths` резолвится внутри workspace песочницы — ошибка
  раскладки ADR-0015 §0.3; лечится `skills.paths: [$SIM/skills]` в конфиге
  (стоит добавить в рецепт README §2).
- `google/gemini-2.0-flash-001` больше не существует на OpenRouter («No
  endpoints found») — vision-модель для S29 выбирать из актуального каталога
  (использован `google/gemini-3.5-flash`).
- Итог по токенам: ~440k суммарно, $0.0000 по метерингу OpenRouter (бесплатные
  тиры моделей).
