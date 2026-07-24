# Поддержка документов (PDF/DOCX/офисные форматы) и изображений

**Дата:** 2026-07-24
**Статус:** одобрено, ждёт реализации
**Скоуп:** внешние executor'ы claude-code и opencode. Codex (без MCP) получает
только bash-путь через docker-образ, если образ для него появится. Нативный
`AgentLoop` — вне скоупа (vision в `ChatMessage` — отдельная работа).

## Проблема

Harness передаёт внешнему агенту задачу только текстом (`AgentLaunch.task`) и
workspace. Ни harness, ни sandbox-образы не умеют:

- извлекать текст из PDF/DOCX/XLSX/PPTX/HTML/EPUB/RTF;
- отдавать изображения модели (у claude есть нативный `Read` для картинок,
  у opencode поддержка зависит от модели/провайдера — гарантий нет);
- OCR'ить сканы.

Сеть sandbox'а изолирована (единственный hop — bridge, ADR-0016 §2), поэтому
скачать конвертер в рантайме агент не может: всё нужное обязано быть либо в
образе, либо на стороне моста.

## Решение: два взаимодополняющих слоя

### Слой 1 — конвертеры в docker-образах

В оба Dockerfile (`docker/agent-claude/Dockerfile`,
`docker/agent-opencode/Dockerfile`) в существующий `apt-get install` слой
добавляются:

| Пакет | Зачем |
|---|---|
| `pandoc` | DOCX/HTML/EPUB/RTF → Markdown через bash |
| `poppler-utils` | `pdftotext` (текстовый слой PDF), `pdftoppm` (PDF → PNG постранично для vision-чтения сканов) |
| `tesseract-ocr` + `tesseract-ocr-rus` + `tesseract-ocr-eng` | классический OCR сканов, дёшево по токенам |

Примерно +150–200 MB на образ. Версии не пинятся — пакеты из Debian stable,
в духе текущей философии образов (дрейф ловят тесты, не пиннинг, ADR-0016 §8).
XLSX/PPTX bash-путём не покрываются — для них слой 2.

Сканы читаются двумя путями на выбор агента: `tesseract` (дёшево, качество
ниже) или `pdftoppm -r 150` → PNG → нативный `Read` / `read_image`
(vision, дороже по токенам, качество выше).

### Слой 2 — MCP-инструменты моста Svarog

Новый модуль `src/svarog_harness/tools/document_tools.py`, регистрация в
`BridgeControl._build_tools()` (`runtime/bridge_control.py`) — по образцу
`read_svarog_docs`.

**`read_document`** — `{path, offset?, limit?}`, путь относительно workspace.
Конвертация в Markdown через библиотеку `markitdown` (Microsoft): PDF, DOCX,
XLSX, PPTX, HTML, EPUB и др. Вывод режется `truncate_text` (backpressure
§6.3), `offset`/`limit` — постраничная выдача длинных документов по строкам
результата. `risk_level=LOW`, `action_type="file.read"`, `is_read_only=True`.
Регистрируется только при импортируемом `markitdown`; иначе инструмент
отсутствует в `tools/list`, `doctor` подсказывает `pip install
svarog-harness[docs]`.

**`read_image`** — `{path}`. Возвращает PNG/JPEG/GIF/WebP как MCP image
content block (base64, стандарт MCP `{"type": "image", "data": ...,
"mimeType": ...}`). Зависимостей нет (stdlib: `base64`, `mimetypes`). Лимит
файла ~5 MB (лимит Anthropic API на изображение) — при превышении понятная
ошибка с подсказкой уменьшить разрешение (`pdftoppm -r 150`). Это даёт vision
opencode'у единым путём; claude может продолжать пользоваться нативным `Read`.

### Инфраструктурные изменения под слой 2

1. **`BridgeControl` получает `workspace_dir: Path`** (прокидывается из
   `run_assembly`/`agent_infra`, где workspace уже известен). Пути обоих
   инструментов резолвятся строго внутри workspace: `resolve()` +
   проверка префикса; `..` и symlink-побеги — fail-closed `ToolError`.
2. **`ToolResult` расширяется** опциональным полем
   `blocks: list[dict] | None`. `handle_mcp` в ветке `tools/call` отдаёт
   `blocks`, если они есть, иначе — текущий одиночный текстовый блок.
   Redaction применяется к тексту как сейчас; на image-блоки не
   распространяется (бинарные данные секретов не содержат — источник
   ограничен workspace).
3. **`pyproject.toml`** — опциональная группа
   `docs = ["markitdown[pdf,docx,xlsx,pptx]"]` по образцу `server`/`mcp`.
   Базовая установка работает без неё (без `read_document`).

### Преамбула агента

В преамбулы адаптеров (рядом с существующими упоминаниями MCP-инструментов в
`claude_code.py` / `opencode.py`) добавляется короткий блок про работу с
документами: когда брать bash-конвертеры (`pandoc`, `pdftotext`, `tesseract`),
когда `read_document` (XLSX/PPTX, единый Markdown-выход), когда
`read_image`/`pdftoppm` (изображения, сканы). Упоминание `read_document` —
условное, только когда инструмент реально зарегистрирован; флаг доступности
прокидывается тем же путём, что `self_docs`.

## Почему так, а не иначе

- **Не сторонний MCP-сервер в образе** (markitdown-mcp и т.п.): +сотни MB в
  каждый образ, два разных конфига подключения (launch-файл у claude,
  state-мерж у opencode), версии вне контроля. Мост уже есть, инструменты в
  нём — Python на хосте, тестируются обычным pytest.
- **Не отдельные библиотеки** (pypdf + python-docx + openpyxl + …): 5–6
  зависимостей и свой рендер Markdown — по сути самописный markitdown, при
  этом RTF/EPUB остаются непокрытыми.
- **Не pandoc на хосте**: появилось бы требование к машине пользователя
  вне `pip install` — harness теряет самодостаточность.

## Обработка ошибок

| Случай | Поведение |
|---|---|
| Файл не найден / вне workspace | `ToolError` с точной причиной, fail-closed |
| Файл больше лимита (`read_image` ~5 MB) | ошибка + подсказка про `pdftoppm -r` |
| Неподдерживаемый формат | ошибка со списком поддерживаемых + подсказка про bash-конвертеры |
| `markitdown` не установлен | инструмента нет в `tools/list`; `doctor` предупреждает |
| Битый/зашифрованный документ | ошибка парсера текстом в `ToolResult.failure`, мост не падает |

## Тестирование

- **Unit (`tests/test_document_tools.py`)**: фикстуры-минифайлы (docx/xlsx/pptx
  генерируются в тесте библиотеками из группы `docs`, PNG — однопиксельный
  вручную), проверка Markdown-выхода, `offset`/`limit`, обрезки, path-escape
  (`..`, symlink), лимита размера изображения, неподдерживаемого формата.
- **Contract (мост)**: `tools/list` содержит новые инструменты при
  установленном markitdown и не содержит `read_document` без него;
  `tools/call read_image` возвращает корректный image content block;
  текстовые инструменты работают как раньше (регресс `blocks=None`).
- **Docker-слой**: ручной smoke — `docker build` + `pandoc --version`,
  `pdftotext -v`, `tesseract --version` внутри контейнера. CI-сборки образов
  нет и сейчас — не добавляем.

## Вне скоупа

- Vision для нативного `AgentLoop` (изображения в `ChatMessage`).
- Автоматический выбор OCR vs vision — решает агент по преамбуле.
- Запись документов (генерация DOCX/PDF агентом).
- Ресайз изображений на мосте (Pillow не добавляем).
