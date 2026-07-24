"""Единичный svarog run зовёт автозахват для DEFAULT-профиля (#1).

Полный e2e для `run` тяжёл (sandbox+LLM); основное покрытие автозахвата —
tests/test_autocapture_runner.py. Здесь фиксируем инвариант профиля: захват
привязан к DEFAULT-run'ам, а DREAM — отдельный профиль без автозахвата.
"""

from svarog_harness.runtime.orchestrator import RunProfile


def test_dream_profile_distinct_from_default() -> None:
    assert RunProfile.DREAM is not RunProfile.DEFAULT
