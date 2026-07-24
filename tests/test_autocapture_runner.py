"""Интеграция автозахвата на TaskRunner (#1): гейт + прямая запись в профиль."""

from pathlib import Path

import pytest

from svarog_harness.config.schema import SvarogConfig
from svarog_harness.llm.provider import CompletionResult, ModelProvider
from svarog_harness.runtime.orchestrator import TaskRunner
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.trace.recorder import TraceRecorder


class _FakeProvider(ModelProvider):
    async def complete(self, messages, tools, *, on_text_delta=None) -> CompletionResult:
        return CompletionResult(content='{"facts": [{"section": "Язык", "fact": "русский"}]}')


def _cfg(tmp_path: Path, *, enabled: bool = True) -> SvarogConfig:
    return SvarogConfig.model_validate(
        {
            "models": {"default": "m", "providers": {"m": {"base_url": "http://x", "model": "m"}}},
            "memory": {"path": str(tmp_path / "memory")},
            "storage": {"db_path": str(tmp_path / "db.sqlite3")},
            "autocapture": {"enabled": enabled},
        }
    )


def _db(cfg: SvarogConfig):
    init_db(cfg.storage.db_path)
    engine = create_engine(cfg.storage.db_path)
    return create_session_factory(engine)()


@pytest.mark.asyncio
async def test_autocapture_disabled_is_noop(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, enabled=False)
    (tmp_path / "memory" / "user").mkdir(parents=True)
    runner = TaskRunner(cfg, tmp_path)
    async with _db(cfg) as db:
        got = await runner.autocapture(db, TraceRecorder(db), "sess", since_turn=0)
    assert got == 0


@pytest.mark.asyncio
async def test_autocapture_writes_fact_to_profile(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, enabled=True)
    mem = tmp_path / "memory"
    (mem / "user").mkdir(parents=True)
    (mem / "user" / "profile.md").write_text("# Профиль\n", encoding="utf-8")
    # memory-репо должен быть git-репозиторием для writer'а
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=mem, check=True)
    subprocess.run(["git", "add", "-A"], cwd=mem, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=mem,
        check=True,
    )

    runner = TaskRunner(cfg, tmp_path)
    monkeypatch.setattr(runner._assembly, "auxiliary_provider", lambda: _FakeProvider())

    async with _db(cfg) as db:
        recorder = TraceRecorder(db)
        run = await recorder.start_run(task="пиши по-русски", autonomy="yolo", model="m")
        await recorder.add_message(run, "assistant", {"content": "ок"})
        await db.commit()
        got = await runner.autocapture(db, recorder, run.session_id, since_turn=0)

    assert got == 1
    assert "русский" in (mem / "user" / "profile.md").read_text(encoding="utf-8")
