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

from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.schema import CuratorConfig
from svarog_harness.skills.curator.state import CuratorStore
from svarog_harness.skills.models import Skill
from svarog_harness.storage.models import SkillLifecycleStatus, utcnow


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
    transitions: list[Transition] = []
    for skill in skills:
        if skill.metadata.provenance != "agent":
            continue  # curator работает только с agent-created (§18.1)
        state = await store.upsert(skill.name, skill.metadata.provenance)
        last_used = await store.last_used(skill.name)
        state.last_used_at = last_used
        if state.pinned:
            continue  # pinned — вне авто-переходов
        # scheduled-задачи защищали бы скилл от архивации (§18.1); их пока нет.
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
