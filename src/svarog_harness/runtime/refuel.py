"""Refuel (§6.10, §20): сериализация состояния задачи в task_state.md.

При достижении порога итераций контекст сбрасывается: состояние задачи
пишется в task_state.md и коммитится (Flow C), новая сессия пересобирает
контекст с нуля из task_state.md + Git. task_state.md строится механически
из хода run'а (без обращения к LLM).
"""

from svarog_harness.llm.provider import ChatMessage

_TASK_STATE_FILE = "task_state.md"


def task_state_path() -> str:
    return _TASK_STATE_FILE


def build_task_state(task: str, messages: list[ChatMessage], iterations: int) -> str:
    """Собрать task_state.md из хода run'а (§20)."""
    tool_calls: list[str] = []
    findings: list[str] = []
    for message in messages:
        if message.role == "assistant":
            for call in message.tool_calls:
                tool_calls.append(call.name)
            if message.content.strip():
                findings.append(message.content.strip())

    used_tools = _counts(tool_calls)
    tool_lines = [f"- {name}: {count}" for name, count in used_tools] or ["- (нет вызовов)"]
    finding_lines = [f"- {f}" for f in findings[-5:]] or ["- (пока нет заметных выводов)"]

    lines = [
        "# Task state",
        "",
        "## Current goal",
        task,
        "",
        "## Progress",
        f"Выполнено итераций: {iterations}.",
        "",
        "## Tools used",
        *tool_lines,
        "",
        "## Important findings",
        *finding_lines,
        "",
        "## Next recommended action",
        "Продолжить выполнение задачи с учётом уже сделанного выше.",
        "",
        "## Open questions / risks",
        "- Проверить, не потеряны ли промежуточные результаты при сбросе контекста.",
    ]
    return "\n".join(lines) + "\n"


def _counts(names: list[str]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items())
