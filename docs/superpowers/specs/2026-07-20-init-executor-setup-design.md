# Расширение `svarog init`: настройка Claude Code и OpenCode как исполнителя

## Контекст

`svarog init` (`src/svarog_harness/cli/main.py:191`, `src/svarog_harness/scaffold.py`)
сегодня настраивает только секцию `models` (нативный OpenAI-совместимый
provider для `AgentLoop`). Секция `executor.external`, которая включает
внешнего агента (Claude Code / OpenCode / Codex) как исполнителя
(ADR-0016), командой вообще не трогается — её нужно дописывать в
`svarog.yaml` руками, включая выбор auth-режима для Claude
(`api-key` / `subscription`, `config/schema.py:318`) и модель/endpoint для
OpenCode.

Схема `ExternalExecutorConfig` хранит только **один** активный внешний
адаптер за раз (`adapter: claude-code | codex | opencode`). Это не меняется
в рамках этой задачи — расширяется только `init`.

## Цель

`svarog init` должен уметь (интерактивно и через флаги для `--no-input`):

1. Настроить Claude Code — выбрать auth-режим (`api-key` или `subscription`
   через `claude setup-token`) и токен, с возможностью пропустить ввод
   значения (сохранить только имя secret-ref, значение — потом через
   `svarog secrets set`).
2. Настроить OpenCode — модель/base_url/api-key, **с опцией переиспользовать
   те же креды, что уже введены для нативного provider'а** (без повторного
   ввода и без создания отдельного secret-ref).
3. Спросить про Claude Code и OpenCode **независимо друг от друга** — можно
   настроить оба, даже если в `executor.external` попадёт только один
   (активный); второй остаётся готовым к переключению.
4. Не менять поведение `init` для тех, кто не просит внешнего исполнителя —
   `executor` в `svarog.yaml` не появляется вовсе, если ни Claude, ни
   OpenCode не были настроены (полная обратная совместимость, существующие
   тесты `tests/test_init.py` не меняют ожидания).

## Интерактивный флоу

После существующих вопросов (путь agent-home → модель/base_url/api-key
нативного provider'а) добавляются два независимых блока:

```
Настроить Claude Code как исполнителя? [y/N]
  → да:
    Режим авторизации [api-key/subscription] (default: api-key)
      api-key:
        API-ключ Anthropic (Enter — пропустить, добавить позже) [hidden]
      subscription:
        OAuth-токен (`claude setup-token`) (Enter — пропустить, добавить позже) [hidden]

Настроить OpenCode как исполнителя? [y/N]
  → да:
    Использовать те же креды, что и для нативного provider'а
    (модель/base_url/api-key)? [Y/n]
      да: модель/base_url/api_key_ref берутся из уже введённых значений
          нативного provider'а — новых вопросов и новых секретов нет.
      нет:
        Модель (default: то, что введено для нативного provider'а)
        Base URL endpoint (default: то же)
        API-ключ (Enter — пропустить, добавить позже) [hidden]

# только если и Claude, и OpenCode подтверждены:
Какой сделать активным исполнителем? [claude-code/opencode] (default: claude-code)
```

Если ни один блок не подтверждён — поведение `init` не меняется вообще.
Если подтверждён только один — он автоматически становится активным,
финальный вопрос не задаётся. Если `--executor` уже передан флагом (в том
числе в интерактивном запуске) — финальный вопрос тоже не задаётся, флаг
используется напрямую; это тот же принцип «флаг перекрывает вопрос», что
уже применяется к `model`/`base_url`/`api_key` (`main.py:220-231`).

## Флаги для `--no-input` / скриптов

Новые опции команды `init`:

- `--executor [native|claude-code|opencode]` — какой адаптер сделать
  активным (`executor.type`/`executor.external.adapter`).
- `--claude-auth [api-key|subscription]`
- `--claude-api-key TEXT`
- `--claude-oauth-token TEXT`
- `--opencode-model TEXT`
- `--opencode-base-url TEXT`
- `--opencode-api-key TEXT`
- `--opencode-same-as-native` / `--opencode-own-creds` — взаимоисключающие
  булевы флаги (по образцу существующей тройки `--yolo/--auto/--supervised`,
  `main.py:455-459` + `_resolve_autonomy`).

**Триггеры блока** (одинаково для интерактива и флагов):
Claude запрашивается, если задан `--executor claude-code` ИЛИ любой из
`--claude-auth/--claude-api-key/--claude-oauth-token`. OpenCode
запрашивается, если задан `--executor opencode` ИЛИ любой из
`--opencode-model/--opencode-base-url/--opencode-api-key/--opencode-same-as-native/--opencode-own-creds`.

**Разрешение конфликтов в `--no-input`:**

- Оба флага `--opencode-same-as-native` и `--opencode-own-creds` заданы
  одновременно → ошибка, exit(1).
- OpenCode запрошен, но ни `--opencode-same-as-native`, ни
  `--opencode-own-creds` не заданы → по умолчанию `same-as-native=True`
  (минимум обязательных флагов для рабочего конфига).
- И Claude, и OpenCode запрошены, `--executor` не задан, `--no-input` →
  ошибка, exit(1): «оба адаптера настроены — уточните `--executor`».
- `--executor native` вместе с любым `--claude-*`/`--opencode-*` флагом →
  ошибка, exit(1) (конфликт намерений, не игнорируем молча).
- `--executor` называет адаптер, для которого не было ни одного отдельного
  флага (например, только `--executor opencode` без других
  `--opencode-*`) — адаптер всё равно настраивается, с дефолтами
  (`same-as-native=True` для opencode; `auth=api-key` без сохранённого
  значения для claude).

## Рендеринг `svarog.yaml`

`scaffold_agent_home` (`scaffold.py`) получает новый опциональный параметр
`executor: ExecutorSetup | None`. Пустой (`None`) — секция `executor` не
рендерится вообще (текущее поведение, байт-в-байт).

Новые dataclass'ы в `scaffold.py`:

```python
@dataclass(frozen=True)
class ClaudeExecutorSetup:
    auth: Literal["api-key", "subscription"]
    api_key_ref: str | None       # None → строка комментируется (auth=api-key без ключа)
    oauth_token_ref: str | None   # всегда задан, если auth=subscription (схема требует)

@dataclass(frozen=True)
class OpencodeExecutorSetup:
    model: str
    base_url: str
    api_key_ref: str | None       # None → строка комментируется

@dataclass(frozen=True)
class ExecutorSetup:
    active: Literal["claude-code", "opencode"]
    claude: ClaudeExecutorSetup | None = None
    opencode: OpencodeExecutorSetup | None = None
```

`active` определяет, какой блок идёт в реальный (не закомментированный)
`executor:`; если второй адаптер тоже был настроен (присутствует в
`claude`/`opencode`, но не совпадает с `active`), под основным блоком
дописывается **комментарий** с готовым альтернативным блоком — переключение
означает раскомментировать и заменить, значения/ссылки на секреты уже на
месте.

Пример (активен `claude-code` в режиме `subscription`, OpenCode тоже
настроен через reuse нативных кредов):

```yaml
executor:
  type: external
  external:
    adapter: claude-code
    image: svarog/agent-claude:latest
    auth: subscription
    oauth_token_ref: CLAUDE_CODE_OAUTH_TOKEN

# OpenCode тоже настроен (креды — те же, что у models.local) и готов —
# чтобы переключиться, замените блок executor выше на:
# executor:
#   type: external
#   external:
#     adapter: opencode
#     image: svarog/agent-opencode:latest
#     model: qwen3-coder
#     base_url: http://localhost:8000/v1
#     api_key_ref: PROVIDER_API_KEY
```

Константы образов/ref-имён (`scaffold.py`, по аналогии с
`DEFAULT_MODEL`/`DEFAULT_API_KEY_REF`):

```python
DEFAULT_CLAUDE_IMAGE = "svarog/agent-claude:latest"
DEFAULT_OPENCODE_IMAGE = "svarog/agent-opencode:latest"
DEFAULT_CLAUDE_API_KEY_REF = "CLAUDE_CODE_KEY"
DEFAULT_CLAUDE_OAUTH_TOKEN_REF = "CLAUDE_CODE_OAUTH_TOKEN"
DEFAULT_OPENCODE_API_KEY_REF = "OPENCODE_API_KEY"
```

## Секреты

После `scaffold_agent_home(...)` в `init()` (`main.py`), рядом с уже
существующим сохранением ключа нативного provider'а:

- Claude: если значение токена/ключа введено — сохранить в
  `FileSecretStore` под `api_key_ref`/`oauth_token_ref` соответствующего
  режима.
- OpenCode: если **не** reuse нативных кредов и значение ключа введено —
  сохранить под `OPENCODE_API_KEY`. При reuse — ничего не сохраняется
  повторно, используется уже существующий ref нативного provider'а (если
  нативный ключ был пропущен и `api_key_ref is None` — строка `api_key_ref`
  в блоке OpenCode комментируется по тому же правилу, что и везде, а не
  считается ошибкой).

Финальное резюме команды (`console.print("agent-home готов...")`) обобщается
со списка из одного возможного напоминания («добавьте ключ: …») до списка
из N напоминаний — по одному на каждый ref, который присутствует в yaml, но
не имеет сохранённого значения (нативная модель, Claude, OpenCode — любая
комбинация).

## Границы (не входит в задачу)

- Не меняется `config/schema.py` — по-прежнему один активный адаптер.
- В интерактиве/флагах не появляется `codex` — пользователь просил именно
  Claude Code и OpenCode; codex остаётся доступен только ручной правкой
  `svarog.yaml`.
- `image`, `enforcement`, `timeout_sec`, `approval_grace_sec`, pricing-поля
  `executor.external` не настраиваются через `init` — остаются на дефолтах
  схемы (как и `image`, взятый константой, а не вопросом).

## Тестирование

Новые тесты в `tests/test_init.py` (mirror существующего стиля,
`CliRunner.invoke` + прямые вызовы `scaffold_agent_home`):

1. `test_init_no_executor_flags_omits_executor_section` — без новых флагов
   `svarog.yaml` не содержит `executor:` (регрессия для обратной
   совместимости).
2. `test_init_claude_api_key_writes_executor_block` — `--executor
   claude-code --claude-auth api-key --claude-api-key sk-x` → в yaml
   `adapter: claude-code`, `auth: api-key`, `api_key_ref: CLAUDE_CODE_KEY`
   (активная строка); ключ в secrets.json, не в yaml.
3. `test_init_claude_subscription_without_token_comments_reminder` —
   `--claude-auth subscription` без `--claude-oauth-token` → `oauth_token_ref:
   CLAUDE_CODE_OAUTH_TOKEN` присутствует (не закомментирован), значения в
   secrets.json нет, в выводе — напоминание сохранить токен.
4. `test_init_opencode_same_as_native_reuses_ref` — `--executor opencode
   --model m --base-url url --api-key sk-x --opencode-same-as-native` →
   `executor.external.model == m`, `base_url == url`, `api_key_ref ==
   PROVIDER_API_KEY` (тот же ref, что у `models.local`), новый
   `OPENCODE_API_KEY` не создаётся.
5. `test_init_opencode_own_creds_writes_separate_ref` —
   `--opencode-own-creds --opencode-model m2 --opencode-api-key sk-y` →
   отдельный `api_key_ref: OPENCODE_API_KEY`, значение `sk-y` в
   secrets.json, не совпадает со значением нативного ключа.
6. `test_init_both_adapters_writes_standby_comment` — заданы и
   `--claude-*`, и `--opencode-*` с явным `--executor claude-code` → активный
   блок `claude-code`, ниже — закомментированный блок с `adapter: opencode`
   и уже проставленными значениями/ref.
7. `test_init_both_adapters_without_executor_flag_errors` — оба блока
   заданы флагами, `--executor` не передан, `--no-input` → exit_code != 0,
   сообщение просит уточнить `--executor`.
8. `test_init_conflicting_opencode_creds_flags_errors` —
   `--opencode-same-as-native --opencode-own-creds` вместе → exit_code != 0.
9. `test_init_executor_native_with_claude_flags_errors` — `--executor native
   --claude-api-key sk-x` вместе → exit_code != 0.

Плюс юнит-тесты на `scaffold_agent_home` напрямую (без CLI) для проверки
точного текста yaml для каждой комбинации `ExecutorSetup` — по аналогии с
`test_scaffold_writes_model_endpoint` / `test_scaffold_config_omits_key_ref_by_default`.
