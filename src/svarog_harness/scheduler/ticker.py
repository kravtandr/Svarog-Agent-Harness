"""Один проход планировщика (блок D §3-§6).

Исполнение задачи приходит колбэком: пакет `scheduler` не импортирует
`runtime`, связывает их CLI. Текущий момент и дайджест конфига — параметры, а
не глобальное состояние.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from svarog_harness.scheduler.store import JobStore


@dataclass(frozen=True)
class JobRunRequest:
    """Что именно исполнить по сработавшей джобе."""

    job_id: str
    task: str
    workspace: str
    autonomy: str


RunJob = Callable[[JobRunRequest], Awaitable[str]]
WorkspaceBusy = Callable[[str], Awaitable[bool]]


async def tick(
    store: JobStore,
    *,
    now: datetime,
    current_digest: str,
    run_job: RunJob,
    workspace_busy: WorkspaceBusy,
) -> list[str]:
    """Отработать джобы, чьё время пришло. Возвращает id отработавших.

    Порядок проверок важен: сначала заморозка прав (§6), потом занятость
    workspace (§4). Джоба с разошедшимся конфигом не должна исполняться даже
    на свободном рабочем дереве.
    """
    done: list[str] = []
    for job in await store.claim_due(now):
        if job.config_digest != current_digest:
            # Fail-closed, как resume при config drift (ADR-0015 §0.4):
            # ослабление конфига не повышает прав уже одобренной джобы.
            await store.disable_with_reason(job, "конфиг изменился с момента одобрения")
            continue
        if await workspace_busy(job.workspace):
            # Не ошибка: workspace занят интерактивной работой. Расписание уже
            # сдвинуто захватом, поэтому джоба вернётся на следующем тике.
            await store.finish(job, status="пропущено: workspace занят", now=now)
            continue
        request = JobRunRequest(
            job_id=job.id, task=job.task, workspace=job.workspace, autonomy=job.autonomy
        )
        try:
            status = await run_job(request)
        except Exception as exc:
            # Широкий except намеренный: это исход джобы, а не отказ
            # планировщика — упавшая задача не должна ронять тик и не
            # выключает джобу. Причина видна в last_status и в trace run'а.
            await store.finish(job, status=f"ошибка: {type(exc).__name__}", now=now)
            continue
        await store.finish(job, status=status, now=now)
        done.append(job.id)
    return done
