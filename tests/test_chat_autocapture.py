"""Автозахват по границе сессии в chat (#1): close() и порог every_n_turns."""

from pathlib import Path

import pytest

from svarog_harness.cli.chat_engine import ChatEngine
from svarog_harness.config.schema import AutonomyMode, SvarogConfig
from svarog_harness.llm.provider import ChatMessage
from svarog_harness.runtime.orchestrator import RunHooks


def _engine(tmp_path: Path, *, enabled: bool = True, every_n_turns: int = 6) -> ChatEngine:
    cfg = SvarogConfig.model_validate(
        {
            "models": {"default": "m", "providers": {"m": {"base_url": "http://x", "model": "m"}}},
            "memory": {"path": str(tmp_path / "memory")},
            "storage": {"db_path": str(tmp_path / "db.sqlite3")},
            "autocapture": {"enabled": enabled, "every_n_turns": every_n_turns},
        }
    )
    return ChatEngine(cfg, tmp_path, AutonomyMode.YOLO, RunHooks())


class _FakeDB:
    async def close(self) -> None:
        return None


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def autocapture(self, db, recorder, session_id, *, since_turn=0) -> int:
        self.calls.append((session_id, since_turn))
        return len(self.calls)


@pytest.mark.asyncio
async def test_close_runs_autocapture_once(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    runner = _FakeRunner()
    engine._runner = runner  # type: ignore[assignment]
    engine._db = _FakeDB()  # type: ignore[assignment]
    engine._recorder = object()  # type: ignore[assignment]
    engine._session_id = "sess"
    engine._history = [ChatMessage(role="user", content="привет")] * 2
    await engine.close()
    assert runner.calls == [("sess", 0)]


@pytest.mark.asyncio
async def test_close_noop_when_disabled(tmp_path: Path) -> None:
    engine = _engine(tmp_path, enabled=False)
    runner = _FakeRunner()
    engine._runner = runner  # type: ignore[assignment]
    engine._db = _FakeDB()  # type: ignore[assignment]
    engine._recorder = object()  # type: ignore[assignment]
    engine._session_id = "sess"
    engine._history = [ChatMessage(role="user", content="привет")] * 2
    await engine.close()
    assert runner.calls == []
