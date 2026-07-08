"""Context Builder v0 (§6.3): системный промпт + задача.

Полная слоеная модель контекста (память, skill cards, retrieval,
compaction) появляется в M3; в M1 контекст — это системные инструкции,
задача пользователя и живая история диалога внутри run'а.
"""

from pathlib import Path

from svarog_harness.llm.provider import ChatMessage

_SYSTEM_PROMPT = """\
Ты — Svarog, автономный агент, выполняющий задачи в рабочей директории (workspace).

Правила:
- Используй доступные tools для чтения и изменения файлов и запуска команд; \
все пути указывай относительно корня workspace.
- Прежде чем менять файлы, изучи текущее состояние (list_dir, read_file, search_files).
- Действуй самостоятельно и доводи задачу до конца; не задавай пользователю вопросов.
- Когда задача выполнена, дай финальный ответ текстом без вызова tools: \
кратко опиши, что сделано и что проверено.

Workspace: {workspace}
"""


def build_initial_messages(task: str, workspace: Path) -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content=_SYSTEM_PROMPT.format(workspace=workspace)),
        ChatMessage(role="user", content=task),
    ]
