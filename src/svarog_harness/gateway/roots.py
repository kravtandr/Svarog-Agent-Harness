"""Реестр корней workspace-сессий (спека 2026-07-30).

JSON в ~/.svarog/workspace-roots.json: известные корни (для «недавних»
пикера) и карты маршрутизации session→root / run→root. Реестр — кэш
маршрутизации, а не источник истины: промах ведёт на default_root
(WorkspaceHub.route), путь сессии дублируется в Session.meta["workspace"].
Битый или отсутствующий файл — пустой реестр; следующая запись лечит его.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class WorkspaceRootsRegistry:
    path: Path

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # атомарная подмена: полузаписанный файл не читается

    def _touch_root(self, data: dict[str, Any], root: Path) -> None:
        """Обновить last_used корня; заодно лениво выкинуть исчезнувшие."""
        # Нормализовать roots перед мутацией: может быть не-dict из битого файла
        roots = data.get("roots")
        roots = roots if isinstance(roots, dict) else {}
        data["roots"] = roots
        for known in list(roots):
            if not Path(known).is_dir():
                del roots[known]
        roots[str(root)] = datetime.now(UTC).isoformat()

    def record_session(self, session_id: str, root: Path) -> None:
        data = self._load()
        # Нормализовать sessions перед мутацией: может быть не-dict из битого файла
        sessions = data.get("sessions")
        sessions = sessions if isinstance(sessions, dict) else {}
        data["sessions"] = sessions
        sessions[session_id] = str(root)
        self._touch_root(data, root)
        self._save(data)

    def record_run(self, run_id: str, root: Path) -> None:
        data = self._load()
        # Нормализовать runs перед мутацией: может быть не-dict из битого файла
        runs = data.get("runs")
        runs = runs if isinstance(runs, dict) else {}
        data["runs"] = runs
        runs[run_id] = str(root)
        self._touch_root(data, root)
        self._save(data)

    def roots(self) -> list[tuple[Path, str]]:
        """Известные корни, свежие сверху (для «недавних» пикера)."""
        items = self._load().get("roots", {})
        if not isinstance(items, dict):
            return []
        ordered = sorted(items.items(), key=lambda kv: str(kv[1]), reverse=True)
        return [(Path(p), str(ts)) for p, ts in ordered]

    def root_of_session(self, session_id: str) -> Path | None:
        sessions = self._load().get("sessions", {})
        if not isinstance(sessions, dict):
            return None
        value = sessions.get(session_id)
        return Path(value) if isinstance(value, str) else None

    def root_of_run(self, run_id: str) -> Path | None:
        runs = self._load().get("runs", {})
        if not isinstance(runs, dict):
            return None
        value = runs.get(run_id)
        return Path(value) if isinstance(value, str) else None

    def roots_with_runs(self) -> set[Path]:
        """Корни с записанными run'ами — обход refuel-супервизора."""
        runs = self._load().get("runs", {})
        if not isinstance(runs, dict):
            return set()
        return {Path(v) for v in runs.values() if isinstance(v, str)}
