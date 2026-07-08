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


def build_initial_messages(
    task: str,
    workspace: Path,
    *,
    skill_cards: str = "",
    memory: str = "",
) -> list[ChatMessage]:
    system = _SYSTEM_PROMPT.format(workspace=workspace)
    if memory:
        # Память — доверенный контекст агента (в отличие от файлов workspace).
        system = f"{system}\n# Память агента\n{memory}\n"
    if skill_cards:
        system = f"{system}\n# {skill_cards}\n"
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=task),
    ]
