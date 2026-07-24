"""Автозахват фактов о пользователе (#1): извлечение и аддитивная сборка.

Aux-LLM по транскрипту сессии выделяет долговечные факты о пользователе и
раскладывает их по секциям профиля. Запись — только в user/profile.md и только
аддитивно (append новой секции / дополнение существующей). Суперседирование
устаревших фактов — задача Dream (#5), не этой фичи. Extractor без tools:
единственный вызов модели со structured output.
"""

import json
import re
from typing import Any

from svarog_harness.llm.provider import ChatMessage, ModelProvider
from svarog_harness.memory.change import MemoryChangeRequest, MemoryOperation
from svarog_harness.memory.profile import KNOWN_SECTIONS
from svarog_harness.memory.sections import parse_sections

_PROFILE_FILE = "user/profile.md"
_FALLBACK_SECTION = "Прочее"

_SYSTEM = (
    "Ты извлекаешь ДОЛГОВЕЧНЫЕ факты о пользователе из диалога для его профиля.\n"
    "Бери только устойчивое: предпочтения, роль, язык общения, тон, расписание,\n"
    "чего не трогать. НЕ бери эфемерное про конкретную задачу и то, что и так есть\n"
    "в коде/репозитории. Верни СТРОГО JSON:\n"
    '{"facts": [{"section": "<одна из: ' + ", ".join(KNOWN_SECTIONS) + '>", '
    '"fact": "<короткая формулировка>"}]}\n'
    "Если ничего долговечного нет — {\"facts\": []}. Только JSON, без пояснений."
)


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _facts_to_changes(
    raw_facts: list[Any], profile_text: str, *, max_facts: int
) -> list[MemoryChangeRequest]:
    sections = parse_sections(profile_text)
    profile_norm = _normalize(profile_text)
    changes: list[MemoryChangeRequest] = []
    for item in raw_facts:
        if len(changes) >= max_facts:
            break
        if not isinstance(item, dict):
            continue
        fact = str(item.get("fact", "")).strip()
        if not fact:
            continue
        section = str(item.get("section", "")).strip()
        if section not in KNOWN_SECTIONS:
            section = _FALLBACK_SECTION
        if _normalize(fact) in profile_norm:
            continue  # дедуп против текущего профиля
        if section in sections:
            body = sections[section]
            new_body = f"{body}\n{fact}" if body else fact
            changes.append(
                MemoryChangeRequest(
                    file=_PROFILE_FILE,
                    operation=MemoryOperation.REPLACE_SECTION,
                    content=new_body,
                    section=section,
                )
            )
        else:
            changes.append(
                MemoryChangeRequest(
                    file=_PROFILE_FILE,
                    operation=MemoryOperation.APPEND,
                    content=f"\n## {section}\n{fact}",
                )
            )
    return changes


def _extract_json(content: str) -> str:
    """Достать JSON из ответа модели: снять markdown-фенсы и обрамляющую прозу.

    Модели часто оборачивают ответ в ```json … ``` или добавляют пояснение —
    берём тело фенса, иначе срез от первой `{` до последней `}`.
    """
    fence = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
    body = fence.group(1) if fence else content
    start, end = body.find("{"), body.rfind("}")
    return body[start : end + 1] if start != -1 and end > start else body


def _parse_payload(content: str) -> list[Any]:
    try:
        data = json.loads(_extract_json(content))
    except (json.JSONDecodeError, TypeError):
        return []
    facts = data.get("facts") if isinstance(data, dict) else None
    return facts if isinstance(facts, list) else []


async def extract_facts(
    transcript: str, profile_text: str, *, provider: ModelProvider, max_facts: int
) -> list[MemoryChangeRequest]:
    """Извлечь долговечные факты из транскрипта → аддитивные заявки в профиль.

    Best-effort: любой сбой модели/парсинга → пустой список, не исключение.
    """
    messages = [
        ChatMessage(role="system", content=_SYSTEM),
        ChatMessage(
            role="user",
            content=f"Текущий профиль:\n{profile_text or '(пусто)'}\n\nДиалог:\n{transcript}",
        ),
    ]
    try:
        result = await provider.complete(messages, [])
    except Exception:
        # Extractor вне критического пути: любой сбой модели → ноль фактов, не исключение.
        return []
    return _facts_to_changes(_parse_payload(result.content), profile_text, max_facts=max_facts)
