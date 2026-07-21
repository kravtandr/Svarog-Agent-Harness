"""Curator слой 1: механический pruning без LLM (§18.1, ADR-0009).

Детерминированные lifecycle-переходы по usage-статистике из trace. Слой 1
применяет их сам (переходы обратимы и ничего не удаляют — противоречило бы
yolo-first гонять их через governance). Инварианты:

* только agent-created скиллы (official/human — вне зоны действия);
* `pinned` выводит скилл из-под авто-переходов;
* якорь `created_at`: свежий скилл не архивируется до первого использования;
* никогда не удаляет — только archived (обратимо, реактивация при использовании).

Скиллы, на которые ссылаются scheduled-задачи, тоже не архивируются (§18.1);
scheduled-задач в текущем срезе нет — точка расширения помечена в коде.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.schema import CuratorConfig
from svarog_harness.skills.curator.state import CuratorStore
from svarog_harness.skills.models import Skill
from svarog_harness.storage.models import (
    CronJob,
    Run,
    SkillLifecycleStatus,
    SkillLoad,
    utcnow,
)


@dataclass(frozen=True)
class Transition:
    skill_name: str
    old: SkillLifecycleStatus
    new: SkillLifecycleStatus
    reason: str


def _target_status(age_days: int, cfg: CuratorConfig) -> SkillLifecycleStatus:
    if age_days >= cfg.archive_after_days:
        return SkillLifecycleStatus.ARCHIVED
    if age_days >= cfg.stale_after_days:
        return SkillLifecycleStatus.STALE
    return SkillLifecycleStatus.ACTIVE


async def prune_layer1(
    db: AsyncSession,
    skills: list[Skill],
    cfg: CuratorConfig,
    *,
    now: datetime | None = None,
) -> list[Transition]:
    """Применить обратимые lifecycle-переходы для agent-created скиллов."""
    now = now or utcnow()
    store = CuratorStore(db)
    protected = await _skills_used_by_enabled_jobs(db)
    transitions: list[Transition] = []
    for skill in skills:
        if skill.metadata.provenance != "agent":
            continue  # curator работает только с agent-created (§18.1)
        state = await store.upsert(skill.name, skill.metadata.provenance)
        last_used = await store.last_used(skill.name)
        state.last_used_at = last_used
        if state.pinned:
            continue  # pinned — вне авто-переходов
        if skill.name in protected:
            # Скилл используется автоматизацией (ADR-0019): редко срабатывающая
            # джоба иначе потеряла бы свой скилл по сроку неактивности (§18.1).
            continue
        anchor = last_used or state.created_at  # якорь new-skill: created_at
        age_days = (now - anchor).days
        target = _target_status(age_days, cfg)
        if target is not state.status:
            transitions.append(
                Transition(
                    skill_name=skill.name,
                    old=state.status,
                    new=target,
                    reason=(
                        f"неактивен {age_days} дн."
                        if target is not SkillLifecycleStatus.ACTIVE
                        else "снова используется"
                    ),
                )
            )
            state.status = target
            state.archived_at = now if target is SkillLifecycleStatus.ARCHIVED else None
    await db.commit()
    return transitions


async def _skills_used_by_enabled_jobs(db: AsyncSession) -> set[str]:
    """Скиллы, загружавшиеся в run'ах включённых джоб планировщика.

    Связь определяется по телеметрии, а не по тексту задачи джобы: гадать,
    какие скиллы понадобятся строке задания, было бы хрупко (ADR-0019 §8).
    """
    enabled = (await db.execute(select(CronJob.id).where(CronJob.enabled.is_(True)))).scalars()
    job_ids = set(enabled)
    if not job_ids:
        return set()

    runs = (await db.execute(select(Run.id, Run.meta))).all()
    run_ids = {run_id for run_id, meta in runs if (meta or {}).get("cron_job_id") in job_ids}
    if not run_ids:
        return set()

    names = (
        await db.execute(select(SkillLoad.skill_name).where(SkillLoad.run_id.in_(run_ids)))
    ).scalars()
    return set(names)
