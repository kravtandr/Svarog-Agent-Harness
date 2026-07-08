"""CuratorStore: lifecycle-состояние скиллов в SQLite (§18.1, ADR-0009).

Usage-телеметрия (когда скилл загружался) живёт в `skill_loads`; здесь —
производный кураторский статус (active/stale/archived + pin). Хранение в
SQLite, а не во frontmatter — тот же принцип «операционные данные вне
человекочитаемого контента», что и sidecar `.usage.json` у hermes.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.storage.models import (
    SkillLifecycleStatus,
    SkillLoad,
    SkillState,
)


class CuratorStore:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self, skill_name: str) -> SkillState | None:
        result = await self._db.execute(
            select(SkillState).where(SkillState.skill_name == skill_name)
        )
        return result.scalar_one_or_none()

    async def upsert(self, skill_name: str, provenance: str) -> SkillState:
        """Вернуть состояние скилла, создав его при первом появлении (якорь created_at)."""
        state = await self.get(skill_name)
        if state is None:
            state = SkillState(skill_name=skill_name, provenance=provenance)
            self._db.add(state)
            await self._db.flush()
        return state

    async def all(self) -> list[SkillState]:
        result = await self._db.execute(select(SkillState).order_by(SkillState.skill_name))
        return list(result.scalars())

    async def archived_names(self) -> set[str]:
        result = await self._db.execute(
            select(SkillState.skill_name).where(SkillState.status == SkillLifecycleStatus.ARCHIVED)
        )
        return set(result.scalars())

    async def last_used(self, skill_name: str) -> datetime | None:
        """Время последней загрузки скилла из журнала SkillLoad."""
        result = await self._db.execute(
            select(func.max(SkillLoad.created_at)).where(SkillLoad.skill_name == skill_name)
        )
        return result.scalar()

    async def set_pinned(
        self, skill_name: str, pinned: bool, *, provenance: str = "agent"
    ) -> SkillState:
        state = await self.upsert(skill_name, provenance)
        state.pinned = pinned
        await self._db.commit()
        return state
