# Выбор исполнителя и модели в поле ввода

**Дата:** 27 июля 2026
**Статус:** утверждён
**Контекст:** веб-интерфейс «Горн» (`docs/superpowers/specs/2026-07-27-web-ui-gorn-design.md`)

## Задача

В поле ввода чата можно менять только автономию. Исполнитель (нативный
цикл или внешний агент) и модель показаны неизменяемым текстом с
подсказкой «Меняется в настройках». Требуется переключать и то и другое
прямо из чата, а список моделей для провайдеров вроде OpenRouter
запрашивать у самого провайдера, а не вести руками в `svarog.yaml`.

## Почему сегодня нельзя

Две независимые причины.

**Сообщение не несёт таких полей.** `POST /sessions/{id}/messages`
принимает только `autonomy`. Исполнитель берётся из
`self.cfg.executor.type` (`gateway/service.py:626`), а `self.cfg` —
снимок, замороженный в конструкторе `GatewayService` вместе с
`TaskRunner` (`gateway/service.py:156`).

**Правка в «Настройках» не действует до перезапуска.** Форма пишет в
`svarog.yaml` и перечитывает файл для показа, но запуски продолжают идти
по startup-снимку. Человеку об этом нигде не сказано.

Заморозка снимка не случайна: ADR-0015 §0.4 запрещает менять конфиг под
работающим запуском. Перечитывать его можно **между** запусками, но не
под ними.

## Ограничения, которые формируют решение

1. `executor.type='external'` требует `sandbox.type='docker'`, fail-closed
   (`runtime/orchestrator.py:229`).
2. `models.default` и `executor.type` входят в security-снимок конфига
   (`runtime/config_snapshot.py:62,73`). При resume хеш сверяется, и
   расхождение отклоняет продолжение (ADR-0015 §0.4). Значит любой
   override обязан переживать resume в неизменном виде.
3. При `executor: external` модель из `models.providers` не участвует:
   трафик агента идёт на `executor.external.base_url`, а поле
   `executor.external.model` учитывает только адаптер `opencode` —
   `claude-code` и `codex` его игнорируют (`config/schema.py:406`).
4. Цены `input_usd_per_mtok`/`output_usd_per_mtok` прибиты к записи
   провайдера, а не к модели. У моделей OpenRouter они отличаются на два
   порядка: подмена модели без подмены цен ломает учёт стоимости.

## Решение

Выбор исполнителя и модели — **свойство сообщения**, как автономия.
`svarog.yaml` не переписывается.

### 1. Контракт

`SendMessageRequest` получает три необязательных поля:

```python
class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1)
    autonomy: AutonomyMode | None = None
    executor: Literal["native", "external"] | None = None
    provider: str | None = None   # имя записи в models.providers
    model: str | None = None      # подмена поля model внутри этой записи
```

`null` во всех трёх — поведение ровно сегодняшнее.

### 2. Деривация конфига — `gateway/overrides.py`

Новый модуль, чистый: без БД, без сети, без файловой системы.

```python
@dataclass(frozen=True)
class RunOverride:
    executor: Literal["native", "external"] | None = None
    provider: str | None = None
    model: str | None = None

    def is_empty(self) -> bool: ...
    def to_meta(self) -> dict[str, str]: ...          # только непустые поля
    @classmethod
    def from_meta(cls, meta: dict[str, object] | None) -> "RunOverride": ...


class OverrideError(Exception):
    """Override несовместим с конфигом; HTTP 422 с этим текстом."""


def apply_override(
    cfg: SvarogConfig,
    ov: RunOverride,
    *,
    prices: tuple[float, float] | None = None,   # (input, output) USD за Mtok
) -> SvarogConfig: ...
```

`prices` приходит снаружи (см. §4) — модуль не ходит в сеть и не знает
про каталог.

`apply_override` строит производный конфиг через `model_copy(update=...)`
— тот же приём, что уже узаконен в `TaskRunner.spawn_child_run`
(`runtime/orchestrator.py:402`). Проверки, каждая со своим текстом:

- `provider` отсутствует в `cfg.models.providers` → `OverrideError` со
  списком известных имён;
- `executor="external"` при `cfg.executor.external is None` →
  `OverrideError` с указанием, что нужна секция `executor.external`;
- `executor="external"` при `cfg.sandbox.type != "docker"` →
  `OverrideError`; иначе то же самое всплывёт из оркестратора, но без
  объяснения, что делать;
- `model` без `provider` применяется к записи `cfg.models.default`.

Ключ `to_meta` — `"override"`, поддерево внутри `Run.meta`.

### 3. Проброс до запуска и переживание resume

`GatewayService.send_message` получает `RunOverride`, строит производный
конфиг и передаёт его в `_runner_for`:

```python
def _runner_for(
    self, workspace: Path, *, cfg: SvarogConfig | None = None,
    run_meta: dict[str, object] | None = None,
) -> TaskRunner
```

Ветка «workspace сервиса → общий `self._runner`» сохраняется только когда
`cfg is None and run_meta is None`: с override нужен свой экземпляр.

`run_meta` — непрозрачный словарь, который `TaskRunner` доносит до
`TraceRecorder.start_run(extra_meta=...)` тем же маршрутом, каким уже
идёт `parent_run_id`: `TaskRunner` → `RunAssembly` → `build_loop` /
`build_external_executor` → `AgentLoop` / `ExternalAgentExecutor` →
`start_run`. `start_run` подмешивает `extra_meta` в `meta` рядом с
`model` и `config_hash`.

Маршрут через конструкторы, а не отложенная запись в `Run.meta` после
старта: `config_hash` считается внутри `start_run`, и override обязан
лежать в той же строке к моменту первого resume. Дописывание «сразу
после» оставляет узкое окно, а речь о fail-closed проверке.

`_runner_for_run(run_id)` читает `RunOverride.from_meta(run.meta)` и
собирает тот же производный конфиг. Тогда `config_digest` при resume
совпадает со снимком старта, и одобрение гейта у запуска с override не
падает в `ConfigDriftError`.

**Тёплые sandbox-слоты.** `_acquire_warm` кеширует runner на `session_id`.
Слот с другим override переиспользовать нельзя — он держит конфиг
прошлого сообщения. Ключ слота становится `(session_id, override)`; при
несовпадении старый слот закрывается и поднимается новый. Цена честная:
смена исполнителя и так требует другого sandbox.

### 4. Каталог моделей — `gateway/catalog.py`

```python
@dataclass(frozen=True)
class ModelCard:
    id: str
    name: str | None
    context_length: int | None
    input_usd_per_mtok: float | None
    output_usd_per_mtok: float | None


async def fetch_models(provider: ProviderConfig, api_key: str | None) -> list[ModelCard]
def parse_models(payload: dict[str, object]) -> list[ModelCard]
```

- URL — `{provider.base_url.rstrip('/')}/models`: ровно то, что сделал бы
  openai-SDK со своим `base_url`. Побочная польза — `base_url` без `/v1`
  даёт видимую ошибку вместо загадочного молчания при запуске.
- Ключ — через `resolve_api_key` из `llm/openai_compatible.py` по
  `host_store` (host-скоуп: провайдер резолвится вне sandbox). Значение
  не логируется и в ответ не попадает.
- `parse_models` терпимый: у OpenRouter есть `data[].name`,
  `context_length`, `pricing.{prompt,completion}` (USD за токен, умножаем
  на 10⁶); у голого OpenAI только `data[].id`. Чего нет — `None`.
  Элементы без `id` пропускаются, а не роняют разбор.
- Таймаут 10 секунд, не `provider.timeout_sec`: 120 секунд для
  выпадающего списка — это зависший интерфейс.

Эндпоинт `GET /models/{provider}` → `list[ModelCardView]`. TTL-кэш 10
минут в сервисе, ключ — имя провайдера. Ошибки: неизвестный провайдер →
404; провайдер не ответил или ответил не-JSON → 502 с причиной.

`GET /models` → список записей `models.providers`: имя, `base_url`,
текущая `model`, признак `is_default`. Клиенту нужен для первого
контрола, и запросов наружу он не делает.

**Цены.** Когда сообщение несёт `model` и каталог знает его цену,
производная запись провайдера получает эти цены вместе с моделью. Цену
берём из того же TTL-кэша; каталог недоступен — оставляем цены из
конфига. Это делает `apply_override` зависимым от каталога, поэтому цены
передаются в него отдельным аргументом `prices: tuple[float, float] |
None`, а не резолвятся внутри: модуль остаётся чистым.

### 5. Интерфейс

Подвал поля ввода: `автономия · исполнитель · провайдер / модель`.

- Исполнитель — `select` из двух значений («нативный цикл», «внешний
  агент»).
- Провайдер — `select` по записям `models.providers`, если их больше
  одной; при единственной записи — текст.
- Модель — кнопка, открывающая список с поиском: у OpenRouter моделей
  несколько сотен, `<select>` не годится. В строке — имя, длина
  контекста, цена за миллион токенов. Поиск фильтрует по `id` и `name`.
- Выбор липкий на чат, живёт в состоянии клиента и уходит явно в каждом
  сообщении. Перезагрузка страницы возвращает значения из конфига —
  сервер про выбор не помнит, и притворяться, что помнит, не нужно.
- При `executor: external` контролы провайдера и модели гаснут с
  подсказкой, что внешний агент ходит к своему провайдеру
  (`executor.external.base_url`).
- Каталог не пришёл — список показывает причину и оставляет модель из
  конфига; отправку это не блокирует.
- Мобильный: список моделей раскрывается листом снизу на всю ширину,
  поле поиска в фокусе.

### 6. «Настройки» перестают требовать перезапуск

После успешного `write_config` сервис перечитывает конфиг и пересобирает
`self._runner`, если ни один запуск не живёт. Тёплые слоты при этом
закрываются: они держат runner со старым конфигом.

Живой запуск есть — конфиг не перечитывается, а `ConfigDiffView`
(ответ `POST /config`) получает поле `restart_required: bool`, и форма
показывает, что правка вступит в силу после завершения текущих запусков. Уже идущие запуски не трогаются
никогда: конфиг под работающим run не меняется, это и есть §0.4.

## Обработка ошибок

| Ситуация | Код | Текст |
|---|---|---|
| Провайдера нет в конфиге | 422 | `OverrideError` со списком известных |
| `external` без секции `executor.external` | 422 | что дописать в конфиг |
| `external` при `sandbox.type != docker` | 422 | требование ADR-0016 |
| Неизвестный провайдер в `GET /models/{p}` | 404 | имя провайдера |
| Провайдер не ответил | 502 | причина от httpx / статус ответа |
| Запуск в этом чате уже идёт | 409 | как сегодня |

## Тесты

**Сервер.**
`apply_override` — все четыре проверки по отдельности; пустой override
возвращает конфиг без изменений. Round-trip `to_meta`/`from_meta`,
включая частично заполненный override и `meta=None`. `parse_models` —
формат OpenRouter с ценами, голый формат OpenAI, элемент без `id`,
мусор вместо `data`. `fetch_models` — на подменённом httpx-транспорте:
успех, 401, не-JSON, таймаут. Эндпоинты: 200 с кэшем (второй вызов не
ходит наружу), 404, 502. `send_message` с override создаёт run с
`meta["override"]`.

**Ключевой интеграционный тест:** запуск с override → гейт → одобрение →
продолжение без `ConfigDriftError`. Без него вся схема §3 держится на
рассуждении, а не на проверке.

Тёплый слот: два сообщения с разным override поднимают разные слоты, с
одинаковым — переиспользуют один.

`write_config` при живом запуске не перечитывает конфиг и возвращает
`restart_required`; без живого — перечитывает и закрывает тёплые слоты.

**Клиент.** Отправка передаёт выбранные значения; выбор липкий между
сообщениями; при `external` контролы модели заблокированы; поиск
фильтрует список; ошибка каталога показывается и не мешает отправить.

## Что не делается

- Выбор модели не пишется в `svarog.yaml`. Файл остаётся тем, что человек
  ведёт руками.
- `executor.external.model` (адаптер `opencode`) из чата не меняется:
  адаптеров три, и только один его читает. Останется в «Настройках».
- Сервер не запоминает последний выбор чата. Понадобится — добавится
  полем в `Session.meta` отдельной работой.
