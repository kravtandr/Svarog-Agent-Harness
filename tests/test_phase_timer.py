"""Накопитель таймингов фаз хода (блок A §5)."""

import pytest

from svarog_harness.runtime import phase_timer as phase_timer_module
from svarog_harness.runtime.phase_timer import PhaseTimer


def _fake_clock(monkeypatch: pytest.MonkeyPatch, ticks: list[float]) -> None:
    """Подменить time.monotonic() детерминированной последовательностью
    отсчётов (Minor 9): без этого `ms >= 0`/`ms >= 500` проходят даже у
    реализации, где длительность всегда ноль, и ничего не доказывают.

    Тики — двоично-точные дроби (1/4, 1/8...), чтобы вычитание в measure()
    не зависело от округления float и итоговые ms проверялись точным числом.
    """
    values = iter(ticks)
    monkeypatch.setattr(phase_timer_module.time, "monotonic", lambda: next(values))


def test_measure_accumulates_time_and_count(monkeypatch: pytest.MonkeyPatch) -> None:
    # Первый measure: 0.0 -> 0.25 (250мс), второй: 0.25 -> 0.75 (500мс).
    _fake_clock(monkeypatch, [0.0, 0.25, 0.25, 0.75])
    timer = PhaseTimer()
    with timer.measure("llm_call"):
        pass
    with timer.measure("llm_call"):
        pass

    meta = timer.as_meta()
    assert meta["llm_call"]["count"] == 2
    assert meta["llm_call"]["ms"] == 750


def test_last_phase_tracks_most_recent(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_clock(monkeypatch, [0.0, 0.125, 0.125, 0.25])
    timer = PhaseTimer()
    with timer.measure("llm_call"):
        pass
    with timer.measure("tool_exec"):
        pass

    assert timer.as_meta()["last"] == "tool_exec"


def test_last_phase_survives_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Фаза, на которой упал ход, остаётся видна — это и есть «где встал run»."""
    _fake_clock(monkeypatch, [0.0, 0.125])
    timer = PhaseTimer()
    try:
        with timer.measure("tool_exec"):
            raise RuntimeError("сбой")
    except RuntimeError:
        pass

    meta = timer.as_meta()
    assert meta["last"] == "tool_exec"
    assert meta["tool_exec"]["count"] == 1
    assert meta["tool_exec"]["ms"] == 125


def test_restore_continues_accumulating_after_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_clock(monkeypatch, [1.0, 1.25])  # 250мс на дополнительный measure
    timer = PhaseTimer()
    timer.restore({"llm_call": {"ms": 500, "count": 2}, "last": "llm_call"})
    with timer.measure("llm_call"):
        pass

    meta = timer.as_meta()
    assert meta["llm_call"]["count"] == 3
    assert meta["llm_call"]["ms"] == 750


def test_restore_ignores_malformed_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    """Чужой или испорченный meta не должен ронять run."""
    _fake_clock(monkeypatch, [0.0, 0.25])
    timer = PhaseTimer()
    timer.restore({"llm_call": "мусор", "last": 42})
    with timer.measure("llm_call"):
        pass

    meta = timer.as_meta()
    assert meta["llm_call"]["count"] == 1
    assert meta["llm_call"]["ms"] == 250


def test_restore_ignores_non_dict_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    """Critical 2: meta целиком не словарь (строка/число) — ранний возврат,
    а не ValueError/TypeError (регрессия на реальный вызов из resume())."""
    _fake_clock(monkeypatch, [0.0, 0.25])
    timer = PhaseTimer()
    timer.restore("мусор целиком")  # type: ignore[arg-type]
    with timer.measure("llm_call"):
        pass

    meta = timer.as_meta()
    assert meta["llm_call"]["count"] == 1
    assert meta["llm_call"]["ms"] == 250
