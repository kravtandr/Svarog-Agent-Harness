"""Context Builder (§6.3): системный промпт + skill cards + память + задача.

Контекст собирается слоями (§6.3): системные инструкции, память проекта
(из memory/, read-only), карточки доступных skills (progressive disclosure
§3.4 — полное содержимое подгружается через read_skill), задача
пользователя. Compaction/retrieval — пост-MVP.
"""

from pathlib import Path

from svarog_harness.llm.provider import ChatMessage

_SYSTEM_PROMPT = """\
Ты — Svarog, автономный агент, выполняющий задачи в рабочей директории (workspace).

Правила:
- Используй доступные tools для чтения и изменения файлов и запуска команд; \
все пути указывай относительно корня workspace.
- Прежде чем менять файлы, изучи текущее состояние (list_dir, read_file, search_files).
- Если для задачи подходит один из доступных skills, сначала загрузи его \
целиком через read_skill и следуй его инструкции.
- Действуй самостоятельно и доводи задачу до конца; не задавай пользователю вопросов.
- Когда задача выполнена, дай финальный ответ текстом без вызова tools: \
кратко опиши, что сделано и что проверено.

Workspace: {workspace}
"""


def _system_prompt(workspace: Path, *, skill_cards: str, memory: str) -> str:
    system = _SYSTEM_PROMPT.format(workspace=workspace)
    if memory:
        # Память — доверенный контекст агента (в отличие от файлов workspace).
        system = f"{system}\n# Память агента\n{memory}\n"
    if skill_cards:
        system = f"{system}\n# {skill_cards}\n"
    return system


def build_initial_messages(
    task: str,
    workspace: Path,
    *,
    skill_cards: str = "",
    memory: str = "",
    history: list[ChatMessage] | None = None,
) -> list[ChatMessage]:
    # history — предыдущий диалог сессии (chat, §6.3 recent conversation).
    return [
        ChatMessage(
            role="system",
            content=_system_prompt(workspace, skill_cards=skill_cards, memory=memory),
        ),
        *(history or []),
        ChatMessage(role="user", content=task),
    ]


def build_refuel_messages(
    task: str,
    workspace: Path,
    task_state: str,
    *,
    skill_cards: str = "",
    memory: str = "",
) -> list[ChatMessage]:
    """Пересобрать контекст после refuel из task_state.md (§6.10) — раздутая
    история отбрасывается, состояние восстанавливается из сохранённого summary."""
    return [
        ChatMessage(
            role="system",
            content=_system_prompt(workspace, skill_cards=skill_cards, memory=memory),
        ),
        ChatMessage(
            role="user",
            content=(
                f"Задача: {task}\n\n"
                f"Работа над задачей была приостановлена и контекст сброшен. "
                f"Ниже — сохранённое состояние (task_state.md). Продолжи с этого места.\n\n"
                f"{task_state}"
            ),
        ),
    ]
