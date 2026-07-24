"""Тесты extractor'а автозахвата (#1): детерминированная сборка и дедуп."""

import pytest

from svarog_harness.llm.provider import CompletionResult, ModelProvider
from svarog_harness.memory.autocapture import _facts_to_changes, extract_facts
from svarog_harness.memory.change import MemoryOperation


def test_new_section_becomes_append() -> None:
    changes = _facts_to_changes(
        [{"section": "Роль", "fact": "бэкенд в Северстали"}], "", max_facts=5
    )
    assert len(changes) == 1
    c = changes[0]
    assert c.file == "user/profile.md"
    assert c.operation is MemoryOperation.APPEND
    assert "## Роль" in c.content and "Северстали" in c.content


def test_existing_section_becomes_additive_replace() -> None:
    profile = "## Роль\nбэкенд\n"
    changes = _facts_to_changes([{"section": "Роль", "fact": "любит Rust"}], profile, max_facts=5)
    assert changes[0].operation is MemoryOperation.REPLACE_SECTION
    assert changes[0].section == "Роль"
    assert "бэкенд" in changes[0].content and "любит Rust" in changes[0].content


def test_dedup_skips_known_fact() -> None:
    profile = "## Роль\nбэкенд в Северстали\n"
    changes = _facts_to_changes(
        [{"section": "Роль", "fact": "бэкенд в Северстали"}], profile, max_facts=5
    )
    assert changes == []


def test_unknown_section_routed_to_prochee() -> None:
    changes = _facts_to_changes([{"section": "Хобби", "fact": "бег"}], "", max_facts=5)
    assert "## Прочее" in changes[0].content


def test_max_facts_cap() -> None:
    facts = [{"section": "Прочее", "fact": f"факт {i}"} for i in range(10)]
    changes = _facts_to_changes(facts, "", max_facts=3)
    assert len(changes) == 3


class _FakeProvider(ModelProvider):
    def __init__(self, payload: str) -> None:
        self._payload = payload

    async def complete(self, messages, tools, *, on_text_delta=None) -> CompletionResult:
        return CompletionResult(content=self._payload)


@pytest.mark.asyncio
async def test_extract_facts_parses_model_json() -> None:
    payload = '{"facts": [{"section": "Язык", "fact": "русский"}]}'
    changes = await extract_facts(
        "user: пиши по-русски", "", provider=_FakeProvider(payload), max_facts=5
    )
    assert len(changes) == 1 and "русский" in changes[0].content


@pytest.mark.asyncio
async def test_extract_facts_tolerates_bad_json() -> None:
    changes = await extract_facts("нечто", "", provider=_FakeProvider("не json"), max_facts=5)
    assert changes == []
