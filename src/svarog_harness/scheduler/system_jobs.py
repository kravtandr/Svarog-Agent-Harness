"""Системные джобы планировщика (блок D §8, ADR-0019).

Заводятся кодом при старте демона, идемпотентно и защищённо: агентский
инструмент их не меняет и не удаляет. Первый потребитель — механический слой
skill-куратора, запуск которого «по интервалу» обещан в TASK.md §18.1, но был
неисполним до появления планировщика.

Второй потребитель — Dream (блок C, ADR-0020). Для него `dream.enabled` —
гейт ТОЛЬКО на заводке: выключить уже заведённую джобу можно через
`svarog cron disable`, и повторный старт демона её обратно не включит.
"""

from datetime import datetime

from svarog_harness.scheduler.schedule import next_run_after
from svarog_harness.scheduler.store import JobStore
from svarog_harness.storage.models import JobOrigin, ScheduleKind

# Имя джобы — её ключ идемпотентности: повторный старт демона находит её по
# имени и происхождению, а не создаёт вторую.
CURATOR_JOB_NAME = "system:skill-curator"
DREAM_JOB_NAME = "system:memory-dream"

_CURATOR_TASK = (
    "Выполни механическое курирование библиотеки скиллов: переведи неиспользуемые "
    "agent-created скиллы в stale и archived по порогам конфигурации, отчёт "
    "сохрани в artifacts/. Ничего не удаляй — только обратимая архивация."
)


# Текст задачи Dream собирается в момент запуска из находок аудита
# (memory/dream.py), поэтому в джобе лежит только маркер: диспетчер узнаёт
# Dream по имени и подставляет актуальную задачу.
_DREAM_TASK = "Консолидация долговременной памяти (Dream, ADR-0020)."


async def ensure_system_jobs(
    store: JobStore,
    *,
    workspace: str,
    autonomy: str,
    config_digest: str,
    now: datetime,
    prune_interval_sec: int,
    dream_enabled: bool = False,
    dream_interval_sec: int = 86_400,
) -> list[str]:
    """Завести недостающие системные джобы; вернуть id созданных.

    Идемпотентно: существующие джобы не трогаются — ни расписание, ни права,
    ни статус. Иначе рестарт демона молча возвращал бы включённой джобу,
    которую человек намеренно выключил.
    """
    existing = {job.name for job in await store.list_jobs() if job.origin is JobOrigin.SYSTEM}
    created: list[str] = []

    if CURATOR_JOB_NAME not in existing:
        spec = str(prune_interval_sec)
        job = await store.create(
            name=CURATOR_JOB_NAME,
            kind=ScheduleKind.EVERY,
            spec=spec,
            tz="UTC",
            task=_CURATOR_TASK,
            workspace=workspace,
            autonomy=autonomy,
            config_digest=config_digest,
            origin=JobOrigin.SYSTEM,
            first_run_at=next_run_after(ScheduleKind.EVERY, spec, "UTC", now),
            protected=True,
        )
        # Системную джобу завёл код, а не агент: approval не требуется, и
        # включать её вручную было бы лишним шагом при каждой установке.
        await store.set_enabled(job, True)
        created.append(job.id)

    if dream_enabled and DREAM_JOB_NAME not in existing:
        spec = str(dream_interval_sec)
        job = await store.create(
            name=DREAM_JOB_NAME,
            kind=ScheduleKind.EVERY,
            spec=spec,
            tz="UTC",
            task=_DREAM_TASK,
            workspace=workspace,
            autonomy=autonomy,
            config_digest=config_digest,
            origin=JobOrigin.SYSTEM,
            first_run_at=next_run_after(ScheduleKind.EVERY, spec, "UTC", now),
            protected=True,
        )
        await store.set_enabled(job, True)
        created.append(job.id)

    return created
