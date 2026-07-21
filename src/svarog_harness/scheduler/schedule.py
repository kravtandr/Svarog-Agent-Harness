"""Расчёт следующего срабатывания джобы (блок D §2).

Чистые функции: текущий момент приходит параметром, к БД и глобальному времени
модуль не обращается — это делает расчёт тестируемым без подмены времени.

Поддержаны два вида расписания; полный cron-синтаксис вне объёма, он потребовал
бы внешней зависимости.
"""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from svarog_harness.storage.models import ScheduleKind

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ScheduleSpecError(ValueError):
    """Некорректные параметры расписания."""


def parse_spec(kind: ScheduleKind, spec: str) -> None:
    """Проверить параметры расписания; ScheduleSpecError — если они негодные."""
    if kind is ScheduleKind.EVERY:
        try:
            seconds = int(spec)
        except ValueError:
            raise ScheduleSpecError(
                f"интервал должен быть числом секунд, получено {spec!r}"
            ) from None
        if seconds <= 0:
            raise ScheduleSpecError(f"интервал должен быть больше нуля, получено {seconds}")
        return
    if _HHMM_RE.match(spec) is None:
        raise ScheduleSpecError(f"время должно быть в формате HH:MM, получено {spec!r}")


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleSpecError(f"неизвестная таймзона: {tz!r}") from None


def next_run_after(kind: ScheduleKind, spec: str, tz: str, now: datetime) -> datetime:
    """Ближайшее срабатывание строго ПОСЛЕ `now`.

    Строгое «после» важно на границе: при расчёте от момента самого
    срабатывания следующее должно уехать на сутки вперёд, иначе джоба
    зациклилась бы на одном и том же моменте.

    Наивное `now` трактуется как UTC — это формат времени в БД проекта
    (`storage.models.utcnow`), и результат возвращается в том же виде, в
    каком пришёл вход. Без явной трактовки `astimezone` посчитал бы наивное
    время локальным системным, и расписание уехало бы на смещение хоста.
    """
    parse_spec(kind, spec)
    zone = _zone(tz)
    naive_input = now.tzinfo is None
    aware = now.replace(tzinfo=UTC) if naive_input else now

    if kind is ScheduleKind.EVERY:
        return now + timedelta(seconds=int(spec))

    hour, minute = (int(part) for part in spec.split(":"))
    local = aware.astimezone(zone)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    result = candidate.astimezone(aware.tzinfo)
    return result.replace(tzinfo=None) if naive_input else result
