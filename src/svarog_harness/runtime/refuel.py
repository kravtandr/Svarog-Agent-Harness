"""Refuel (§6.10, §20): сериализация состояния задачи в task_state.md.

При достижении порога итераций контекст сбрасывается: состояние задачи
пишется в task_state.md и коммитится (Flow C), новая сессия пересобирает
контекст с нуля из task_state.md + Git. task_state.md строится механически
из хода run'а (без обращения к LLM).

Прогресс НАКАПЛИВАЕТСЯ между раундами (регрессия S19): история сегмента при
сбросе обнуляется, поэтому считать по ней каждый раз заново означало бы
стирать всё сделанное до предыдущего сброса — агент видел бы «сделано ничего»
и завершал незаконченную задачу. Накопитель живёт в LoopState и переживает
checkpoint.
"""

import json

from svarog_harness.llm.provider import ChatMessage

_TASK_STATE_FILE = "task_state.md"
# Сколько последних выводов и файлов показывать: task_state.md идёт в контекст
# целиком, поэтому он обязан оставаться компактным.
_MAX_FINDINGS = 8
_MAX_FILES = 20
# Tools, чей аргумент `path` стоит запомнить: агенту после сброса важнее всего
# знать, что уже создано.
_FILE_TOOLS = frozenset({"write_file", "edit_file"})


def task_state_path() -> str:
    return _TASK_STATE_FILE


def segment_progress(
    messages: list[ChatMessage],
) -> tuple[dict[str, int], list[str], list[str]]:
    """Вынуть из сегмента истории счётчики вызовов, выводы и затронутые файлы."""
    usage: dict[str, int] = {}
    findings: list[str] = []
    files: list[str] = []
    for message in messages:
        if message.role != "assistant":
            continue
        for call in message.tool_calls:
            usage[call.name] = usage.get(call.name, 0) + 1
            if call.name in _FILE_TOOLS:
                path = _path_argument(call.arguments_json)
                if path is not None and path not in files:
                    files.append(path)
        if message.content.strip():
            findings.append(message.content.strip())
    return usage, findings, files


def merge_progress(
    *,
    tool_usage: dict[str, int],
    findings: list[str],
    touched_files: list[str],
    segment: tuple[dict[str, int], list[str], list[str]],
) -> tuple[dict[str, int], list[str], list[str]]:
    """Слить прогресс сегмента в накопитель, соблюдая потолки."""
    seg_usage, seg_findings, seg_files = segment
    merged_usage = dict(tool_usage)
    for name, count in seg_usage.items():
        merged_usage[name] = merged_usage.get(name, 0) + count

    merged_findings = [*findings, *seg_findings][-_MAX_FINDINGS:]
    merged_files = list(touched_files)
    for path in seg_files:
        if path not in merged_files:
            merged_files.append(path)
    return merged_usage, merged_findings, merged_files[-_MAX_FILES:]


def build_task_state(
    task: str,
    *,
    iterations: int,
    tool_usage: dict[str, int],
    findings: list[str],
    touched_files: list[str],
    plan: list[dict[str, str]] | None = None,
) -> str:
    """Собрать task_state.md из накопленного прогресса run'а (§20)."""
    tool_lines = [f"- {name}: {count}" for name, count in sorted(tool_usage.items())] or [
        "- (нет вызовов)"
    ]
    finding_lines = [f"- {f}" for f in findings] or ["- (пока нет заметных выводов)"]
    file_lines = [f"- {path}" for path in touched_files] or ["- (файлы не менялись)"]
    plan_lines = _plan_lines(plan or [])

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
        "## Files created or changed",
        *file_lines,
        "",
        "## Important findings",
        *finding_lines,
        "",
        "## Current plan",
        *plan_lines,
        "",
        "## Next recommended action",
        "Продолжить выполнение задачи с учётом уже сделанного выше. Сверь список "
        "созданных файлов с целью: если из требуемого сделано не всё, продолжай, "
        "а не отчитывайся о завершении.",
        "",
        "## Open questions / risks",
        "- Проверить, не потеряны ли промежуточные результаты при сбросе контекста.",
    ]
    return "\n".join(lines) + "\n"


def _path_argument(arguments_json: str) -> str | None:
    """Достать `path` из аргументов вызова; кривой JSON состояние не роняет."""
    try:
        parsed = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    path = parsed.get("path")
    return path if isinstance(path, str) and path else None


def _plan_lines(plan: list[dict[str, str]]) -> list[str]:
    if not plan:
        return ["- (план не использовался)"]
    return [
        f"- [{item.get('status', '')}] {item.get('id', '')}: {item.get('text', '')}"
        for item in plan
    ]
