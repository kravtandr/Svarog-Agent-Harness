# Фиксы блокеров симуляционной кампании 21.07.2026 — дизайн

Источник требований: кампания agent-based симуляции 21.07.2026 (статусы
записаны в `simulation/scenarios.md`). Цель: все сценарии группы cloud-executor
(S11–S16) и блоков A/B/D/E отрабатывают зелёным на обеих осях (opencode,
claude-code); alpha-блокеры закрыты.

Решения приняты с пользователем 21.07.2026:

* Блокер №1 (brainstorming съедает headless-деливерабл) закрываем НЕ
  подавлением скиллов, а **эскалацией вопроса наверх**: вопрос агента доезжает
  до пользователя, как в обычной интерактивной оболочке. Вариант **A,
  MCP-first** (см. §1); эвристика-фолбэк (вариант B) — вне объёма, до данных
  перепрогона.
* Блокер №4 (хвост S19 на gpt-oss-120b) закрываем **сменой дефолтной модели**,
  без инвестиций в runtime-обходы; фикс reasoning-канала ветки
  `fix-reasoning-channel` остаётся (он про честность и диагностику).
* В объём входят также: честность/память opencode через MCP, гейт до Flow C
  (S15a), чистка сирот в `svarog doctor`, валидатор `base_url`.

---

## 1. Эскалация вопросов пользователю (блокер №1, S11/S13)

### Существующая механика (переиспользуем, не строим заново)

* Мост уже выдаёт MCP-tools `ask_user` / `request_approval`
  (`bridge_control.py`): decision cache → grace-ожидание → suspend run с
  pending `ApprovalRequest` (`external.py` обрабатывает suspend для внешних
  агентов); ответ записывается `record_gate_answer`, run продолжается
  `svarog resume` (та же agent-сессия — resume по `ses_…` доказан S13).
* Claude-code получает мост через `--mcp-config` (streamable HTTP,
  `{"type": "http", "url": ".../svarog/mcp", "headers": {Authorization:
  Bearer …}}`, `agent_infra.py::prepare_launch`).
* CLI-ответ на вопрос существует: `_interactive_approvals` →
  `_answer_question_interactive` (§6.5).

### Что добавляем

**1a. Spike: MCP-клиент OpenCode ↔ мост Svarog.** OpenCode поддерживает
remote-MCP в конфиге (`mcp.<name>: {type: "remote", url, headers, enabled}`),
а managed `opencode.jsonc` пишет Svarog (`OpencodeAdapter.provider_files`).
Спайк: поднять мост, вручную прописать remote-MCP в конфиг контейнера,
проверить list_tools + вызов `remember`/`ask_user`. Комментарий «MCP-конфиг
OpenCode не совместим с HTTP-bridge» в `opencode.py:41` считаем гипотезой,
спайк её подтверждает или опровергает. Если несовместимо (протокол/версия) —
СТОП, возврат к пользователю с вариантом B (эвристика waiting_input).

**1b. Провод MCP в адаптер opencode** (после зелёного спайка):

* `OpencodeAdapter.capabilities()` → `mcp=True` (hooks остаётся False —
  supervised по-прежнему только claude-code, валидатор S15(a) не меняется).
* Секция `mcp` в managed `opencode.jsonc` с URL моста и Bearer-токеном.
  Токен и URL известны только в `agent_infra.prepare_launch` — контракт
  адаптера расширяем НОВЫМ опциональным методом
  `mcp_client_config(url: str, token: str) -> dict[rel_path, patch]`
  (дефолт — пусто), который `prepare_launch` вызывает при `mcp=True` и
  мёржит в state-файлы. Поведение claude-code (`--mcp-config` + strict)
  не меняется — его метод возвращает пусто.
* Egress-периметр: убедиться, что relay/internal-сеть пропускает MCP-трафик
  контейнера к мосту (тот же путь, что LLM-прокси).

**1c. Инструкция агентам — вопросы только через ask_user.** В
`context_files()` обоих адаптеров (CLAUDE.md / AGENTS.md) добавить блок:

> Нужен ответ или решение человека — вызывай MCP-tool `ask_user` и жди
> результата. НИКОГДА не завершай run текстом-вопросом: в headless-режиме
> на него некому ответить, run будет засчитан проваленным. Для творческих
> задач предпочитай разумные дефолты; `ask_user` — только когда выбор
> реально блокирует работу.

Superpowers-бандл в образах не трогаем: brainstorming может задавать свои
вопросы — теперь они эскалируются, а не улетают в пустоту.

**1d. Headless UX.** `svarog run` при `waiting_approval` с payload-вопросом
уже печатает подсказку про `svarog approvals` (exit=3). Добавить
неинтерактивную команду `svarog approvals answer <id> "<текст>"` (тонкая
обёртка над `record_gate_answer`; сейчас ответ — только интерактивный
prompt в `_answer_question_interactive`), чтобы вопрос можно было закрыть
из скриптов и другого терминала.

### Критерии готовности §1

* S11 (оба адаптера, P1+P5): либо деливерабл создан сразу, либо run уходит в
  `waiting_approval` с внятным вопросом; после `approvals answer` + `resume`
  деливерабл создан. «completed без файла» — 0 случаев.
* S13 (opencode): a.md создан, кодовое слово названо, тот же `ses_…`.
* S12 (opencode): «запомни» → настоящий `mcp remember` (см. §4).

---

## 2. Flow C: не трогать project-конфиг и не мусорить ветками (S11 Watch(6), S15a)

* `gitflow/repo.py:139` (`git add -A`) — исключить `svarog.yaml` workspace'а
  из ВСЕХ автокоммитов Flow C (и стартового коммита task-ветки, и
  auto-commit результата): `git add -A -- ':!svarog.yaml'` либо explicit
  pathspec-фильтр. Конфиг с именами секретов не должен попадать в диф run'а,
  и `checkout master` не должен удалять его из рабочего дерева
  (в кампании это трижды роняло следующий запуск «конфигурация не найдена»).
* S15a: гейт `assert_external_autonomy_supported` (и sandbox-гейт) вызывать
  ДО prepare workspace / создания task-ветки — при отказе рабочее дерево
  остаётся на исходной ветке, мусорных `svarog/*` не появляется.
* Регрессионные юниты: (а) после run в task-ветке нет `svarog.yaml`;
  (б) после отказа гейта supervised+opencode ветка не создана.

## 3. Смена дефолтной модели (блокер №4, S19)

Bake-off вместо угадывания: драйвер S19 (4 файла + index) × 2 прогона на
2–3 кандидатах OpenRouter (openai-совместимых, с нормальным tool-calling,
без reasoning-протечек; кандидаты уточняются на месте по доступности, напр.
`qwen/qwen3-coder`, `deepseek/deepseek-chat`, `z-ai/glm-4.7`). Критерий:
`completed` с финальным ответом 2/2, все файлы созданы, цена сопоставима.
Победитель прописывается как дефолт в: `scaffold.py` (svarog init),
`simulation/README.md` §2, `simulation/scenarios.md` (секция группы).
gpt-oss-120b остаётся поддержанным (фикс reasoning-канала в деле), но не
дефолтом.

## 4. Память opencode = память Svarog (блокер №3, S12)

С MCP из §1 у opencode появляется настоящий write-канал. Повторяем паттерн
claude-code:

* `context_files()` opencode — блок «Память» как в `claude_code.py`:
  единственный источник истины — Svarog; запоминать — только
  `mcp__svarog__remember` (читать — `read_memory`); НЕ писать факты в файлы
  workspace и НЕ вести свою локальную «память» в `~/.local/share/opencode`.
* Аналога `CLAUDE_CODE_DISABLE_AUTO_MEMORY` у OpenCode нет (его стейт — это
  сессии, они нужны для resume); «отключение внутренней памяти» реализуется
  инструкцией + проверкой S12 (суррогаты в workspace/стейте = FAIL).
* Фолбэк (если спайк §1a провалится): честная строка в AGENTS.md —
  «долговременной памяти у тебя НЕТ; на „запомни“ отвечай честно и предлагай
  native-режим», и S12 Judge остаётся проверкой честности, а не записи.

## 5. `svarog doctor`: чистка legacy-сирот

Ресурсы с меткой `svarog-agent=1`, но БЕЗ `svarog-owner-pid` (созданы до
reaper'а) сейчас не подметаются никогда — в кампании 4 таких контейнера
роняли `tests/test_external_docker.py` до ручного `docker rm`. В
`svarog doctor` добавить шаг: найти контейнеры/сети `svarog-agent=1` без
owner-метки или с мёртвым owner-pid, показать список, удалить (с
подтверждением; `--yes` для скриптов).

## 6. Валидатор `base_url` внешнего executor'а

Два тихих отказа из заметок группы S11–S16 (по упавшему run'у на каждый):

* адаптер с `wire_format == "openai"` (opencode/codex) + дефолтный
  `base_url == "https://api.anthropic.com"` → ошибка конфигурации с текстом
  «wire=openai требует явный base_url OpenAI-совместимого провайдера»;
* `base_url` внешнего executor'а, оканчивающийся на `/v1` → ошибка «адаптер
  добавляет /v1 сам; уберите суффикс» (в отличие от
  `models.providers.*.base_url`, где `/v1` нужен — валидатор различает эти
  два поля).

Реализация — `model_validator` в `ExternalExecutorConfig` (schema.py), где
известен адаптер. Юниты на оба случая + на то, что anthropic-дефолт с
claude-code остаётся валиден.

## 7. Верификация (после всех фиксов)

1. `uv run pytest -q` — зелёный (включая новые юниты §2/§5/§6).
2. Перепрогон сценариев по рецепту `simulation/README.md`:
   * S11 P1+P5 × оба адаптера (критерий §1);
   * S12 × opencode (remember через MCP, curate чист) и контроль честности;
   * S13 × opencode (a.md + кодовое слово);
   * S15 a/b/c (отказ ДО Flow C, веток нет);
   * S19 × 2 на новой дефолтной модели (`completed` 2/2);
   * дымово: S14, S16 — не сломаны изменением capabilities opencode.
3. Обновить статусы в `simulation/scenarios.md`; на каждый новый найденный
   баг — регрессионный сценарий.

## Порядок и зависимости

Спайк §1a — первым (полдня, определяет ветвление §1/§4). Дальше блоки
независимы: §2, §3, §5, §6 можно вести параллельно с §1b–d. Верификация §7 —
последней, единым перепрогоном.

## Вне объёма

* Вариант B (эвристика question-shaped final answer → `waiting_input`).
* Hooks/supervised для opencode (остаётся fail-closed на claude-code).
* Инжекция chat-истории в первый opencode-ход после переключения
  (S14 Watch(1)) и кросс-адаптерный `last_agent_session` (S14 Watch(2)).
* Материализация файлов child-run'а в родительский workspace (S16 нюанс).
