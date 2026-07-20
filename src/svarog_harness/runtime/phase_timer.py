"""Тайминги фаз хода (блок A §5).

Отвечает на вопрос «где встал run» и «куда ушло время», не перестраивая
управление в AgentLoop: фазы — это уже существующие участки цикла. Агрегат
живёт в Run.meta, поэтому переживает resume и не требует миграции.

approval_wait измеряется отдельной фазой и не смешивается с остальными:
ожидание решения человека измеряется часами и исказило бы любую сумму.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class PhaseTimer:
    def __init__(self) -> None:
        self._phases: dict[str, dict[str, int]] = {}
        self._last: str = ""

    @contextmanager
    def measure(self, phase: str) -> Iterator[None]:
        """Замерить участок; фаза засчитывается даже при исключении внутри."""
        started = time.monotonic()
        self._last = phase
        try:
            yield
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            entry = self._phases.setdefault(phase, {"ms": 0, "count": 0})
            entry["ms"] += elapsed_ms
            entry["count"] += 1

    def as_meta(self) -> dict[str, Any]:
        """Снимок для Run.meta['phases']."""
        meta: dict[str, Any] = {name: dict(entry) for name, entry in self._phases.items()}
        meta["last"] = self._last
        return meta

    def restore(self, meta: dict[str, Any]) -> None:
        """Восстановить агрегат после resume; испорченные записи пропускаются."""
        for name, entry in meta.items():
            if name == "last":
                if isinstance(entry, str):
                    self._last = entry
                continue
            if not isinstance(entry, dict):
                continue
            ms = entry.get("ms")
            count = entry.get("count")
            if isinstance(ms, int) and isinstance(count, int):
                self._phases[name] = {"ms": ms, "count": count}
