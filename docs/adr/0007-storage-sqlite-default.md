# ADR-0007: Хранилища — Git + SQLite обязательны, остальное pluggable

## Статус

Принято

## Контекст

ТЗ перечисляет Git, SQLite/Postgres, Redis и Qdrant. Если все четыре обязательны, `svarog init` начинается с docker-compose на пять сервисов — это противоречит требованию простой локальной установки и режиму local trusted.

## Решение

Базовая установка работает **только на Git + SQLite**, ноль внешних сервисов. Все остальное — backends за интерфейсами:

| Интерфейс | Default (MVP) | Server/corporate |
|---|---|---|
| `Database` | SQLite (SQLAlchemy 2.0 async) | Postgres |
| `QueueBackend` | таблица SQLite (переживает рестарт) | Redis |
| `LockBackend` | файловый advisory-lock (`fcntl.flock`, `FileLockBackend`) — сериализует memory-writer между процессами на одной машине | Redis (multi-machine) |
| `EventStream` (streaming событий) | in-process pub/sub | Redis pub/sub |
| `VectorBackend` | выключен (retrieval по памяти — grep/структура файлов) | Qdrant (рекомендуемый), интерфейс допускает другие |

Выбор backend'ов — секция `storage:` в `svarog.yaml`.

## Последствия

* `svarog init` работает на чистой машине с Python и Docker (Docker нужен только для sandbox; local trusted mode работает вообще без него).
* Семантический retrieval — деградирует изящно: без vector DB память ищется по структуре и grep, что для персонального объема памяти достаточно.
* Один процесс — ограничение MVP; переход на multi-process (gateway + workers) не меняет код компонентов, только конфигурацию backends. Memory-writer уже сериализован межпроцессным `FileLockBackend` (ADR-0004), поэтому параллельные интерфейсы на одной машине не конфликтуют на git-репозитории памяти без Redis.
* SQLAlchemy как ORM-слой дает SQLite→Postgres без переписывания.
