"""Инвариант истории перед вызовом модели (блок A §1).

Svarog не чинит историю, как это делают харнессы без write-ahead: у него
незакрытых tool-вызовов не бывает по построению — `pending_tool_calls`
попадают в checkpoint до исполнения и доисполняются первыми при resume.
Поэтому нарушение пар «вызов ↔ результат» означает баг в логике loop'а, и
правильная реакция — упасть громко, а не подставить заглушку и замаскировать
его.
"""

from collections import Counter

from svarog_harness.llm.provider import ChatMessage


class HistoryInvariantError(RuntimeError):
    """История нарушает контракт диалога — вызов модели не выполняется."""


def assert_history_valid(messages: list[ChatMessage]) -> None:
    """Проверить историю перед отправкой модели; нарушение — HistoryInvariantError.

    Счёт по `tool_call_id`, а не множество: небрежный сервер может
    переиспользовать id между ходами (разные объявления в разных
    assistant-сообщениях) — это легально, пока число объявлений и число
    результатов для каждого id совпадает на каждый момент истории.
    """
    if not messages:
        raise HistoryInvariantError("пустая история: нечего отправлять модели")
    if messages[0].role != "system":
        raise HistoryInvariantError(
            f"первое сообщение истории должно быть system, получено {messages[0].role!r}"
        )

    announced: Counter[str] = Counter()
    answered: Counter[str] = Counter()
    for index, message in enumerate(messages):
        if message.role == "assistant":
            for call in message.tool_calls:
                if not call.name:
                    raise HistoryInvariantError(
                        f"сообщение [{index}]: tool call {call.id!r} имеет пустое имя"
                    )
                announced[call.id] += 1
        elif message.role == "tool":
            call_id = message.tool_call_id
            if call_id is None:
                raise HistoryInvariantError(f"сообщение [{index}]: tool-сообщение без tool_call_id")
            answered[call_id] += 1
            if answered[call_id] > announced[call_id]:
                if announced[call_id] == 0:
                    raise HistoryInvariantError(
                        f"сообщение [{index}]: результат ссылается на неизвестный "
                        f"tool_call_id {call_id!r}"
                    )
                raise HistoryInvariantError(
                    f"сообщение [{index}]: повторный результат для tool_call_id "
                    f"{call_id!r} — объявлений: {announced[call_id]}, "
                    f"результатов: {answered[call_id]}"
                )

    missing = sorted(call_id for call_id in announced if announced[call_id] > answered[call_id])
    if missing:
        raise HistoryInvariantError(
            f"tool call без результата: {', '.join(missing)} — "
            f"баг loop'а (write-ahead должен был доисполнить вызов)"
        )
