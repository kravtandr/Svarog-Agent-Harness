"""Накопитель таймингов фаз хода (блок A §5)."""

from svarog_harness.runtime.phase_timer import PhaseTimer


def test_measure_accumulates_time_and_count() -> None:
    timer = PhaseTimer()
    with timer.measure("llm_call"):
        pass
    with timer.measure("llm_call"):
        pass

    meta = timer.as_meta()
    assert meta["llm_call"]["count"] == 2
    assert meta["llm_call"]["ms"] >= 0


def test_last_phase_tracks_most_recent() -> None:
    timer = PhaseTimer()
    with timer.measure("llm_call"):
        pass
    with timer.measure("tool_exec"):
        pass

    assert timer.as_meta()["last"] == "tool_exec"


def test_last_phase_survives_exception() -> None:
    """Фаза, на которой упал ход, остаётся видна — это и есть «где встал run»."""
    timer = PhaseTimer()
    try:
        with timer.measure("tool_exec"):
            raise RuntimeError("сбой")
    except RuntimeError:
        pass

    meta = timer.as_meta()
    assert meta["last"] == "tool_exec"
    assert meta["tool_exec"]["count"] == 1


def test_restore_continues_accumulating_after_resume() -> None:
    timer = PhaseTimer()
    timer.restore({"llm_call": {"ms": 500, "count": 2}, "last": "llm_call"})
    with timer.measure("llm_call"):
        pass

    meta = timer.as_meta()
    assert meta["llm_call"]["count"] == 3
    assert meta["llm_call"]["ms"] >= 500


def test_restore_ignores_malformed_meta() -> None:
    """Чужой или испорченный meta не должен ронять run."""
    timer = PhaseTimer()
    timer.restore({"llm_call": "мусор", "last": 42})
    with timer.measure("llm_call"):
        pass

    assert timer.as_meta()["llm_call"]["count"] == 1
