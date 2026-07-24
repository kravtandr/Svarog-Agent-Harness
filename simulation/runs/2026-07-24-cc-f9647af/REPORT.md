# Прогон 2026-07-24 (зеркало) — документы и изображения (S27–S29, claude-code)

**Цель:** зеркальный прогон фичи «документы и изображения» (spec 2026-07-24,
main `f9647af`) на **claude-code** — вторая ось сравнения с opencode
(прогон 2026-07-24-24a73bd). Главный вопрос: рендерит ли MCP-клиент
claude-code image content block, который OpenCode терял (S29 остался жёлтым).

**Среда:** рецепт README §2 + cloud-executor; образ
`svarog/agent-claude:latest` пересобран после мерджа (pandoc 2.17,
tesseract 5.3 rus+eng, Claude Code 2.1.218); auth `subscription`
(`oauth_token_ref: CLAUDE_CODE_OAUTH_TOKEN`); модель — из подписки Claude
(vision-capable по умолчанию); `svarog doctor` → `document-tools: ok`.

| Сценарий | Статус | Прогоны | Run'ы | Против opencode |
|---|---|---|---|---|
| S27 read_document (XLSX) | **ЗЕЛЁНЫЙ** | 1/1 | 288978a9 | так же зелёный |
| S28 bash-конвертер (RTF) | **ЗЕЛЁНЫЙ** | 1/1 | 216fa5bc | так же зелёный (после фикса 24a73bd) |
| S29 read_image (vision) | **ЗЕЛЁНЫЙ** | 2/2 | 7af52f8e, 44ccac33 | **opencode ЖЁЛТЫЙ — здесь зелёный** |

Все run'ы: `executor=external`, `adapter=claude-code`.

## S29 — главный результат: vision работает

На claude-code `mcp__svarog__read_image` — **полноценный vision-путь**. В trace
обоих прогонов **единственный** tool_call `mcp__svarog__read_image`, без
единого bash: модель сразу отвечает «сплошной красный квадрат #FF0000»
(прогон 1) / «сплошной красный квадрат, без других элементов» (прогон 2),
2 итерации.

Контраст с opencode лобовой: там после «read_image: ok» модель картинку не
видела и агент 5 ходов декодировал PNG вручную (node+zlib), либо завершался
без ответа. Здесь — ноль форензики.

**Вывод по слою моста:** реализация content blocks в `tools/call`
(`ToolResult.blocks` → `handle_mcp`) **корректна end-to-end** — image-блок
доходит до модели и она видит пиксели. Жёлтый S29 на opencode локализован в
его MCP-клиенте (OpenCode 1.18.4 не пробрасывает image content block в
контент модели), это не дефект Svarog. Кандидат остаётся: issue upstream
OpenCode.

## S27 — read_document (XLSX)

1/1 PASS: единственный `mcp__svarog__read_document` → Markdown-таблица с
маркером `выручка-морж-7742`; агент дополнительно заметил, что маркер
выглядит синтетическим. Идентично поведению opencode.

## S28 — read_document error-path + pandoc образа

1/1 PASS, чистая траектория (trace: `Bash find` → `read_document(rtf)` ошибка
→ `Bash pandoc -f rtf -t markdown`): ошибка моста со списком форматов ведёт
агента в pandoc, ответ содержит `SVAROG-MARKER-4217` и все факты. В отличие
от первого opencode-прогона (до фикса 24a73bd) — без блужданий по
контейнерным путям и без висячего approval: фикс нормализации `/workspace/…`
здесь тоже подтверждён.

## Сводка по фиче

| Возможность | opencode | claude-code |
|---|---|---|
| read_document (офисные форматы через мост) | ✅ | ✅ |
| bash-конвертеры образа (pandoc/pdftotext/tesseract) | ✅ | ✅ |
| read_image как vision-путь | ⚠️ мост ок, MCP-клиент не рендерит | ✅ полноценный |
| нативное чтение изображений | — | ✅ (свой Read, здесь не тестировался) |

Слой Svarog (мост + образы) подтверждён на обоих адаптерах. Единственное
открытое место — vision на opencode, ограничение вне Svarog.

## Заметки прогона

- Стоимость (subscription): ~$0.5–0.8 оценочно на 4 run'а по метрике CLI
  (реальный метеринг $0.0000 — pass-through подписки).
- Образ agent-claude требовал пересборки: тот, что стоял, был старше мерджа
  (без pandoc). После пересборки — все конвертеры на месте.
