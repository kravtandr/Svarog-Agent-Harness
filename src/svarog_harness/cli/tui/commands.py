"""Слэш-команды chat-TUI: реестр, парсинг и автодополнение."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommand:
    name: str  # без слэша: "help"
    usage: str  # как показывать в дропдауне: "/fork <ref>"
    help: str


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("help", "/help", "показать команды и горячие клавиши"),
    SlashCommand("new", "/new", "новая сессия с чистой историей"),
    SlashCommand("sessions", "/sessions", "выбрать сессию: продолжить или форкнуть"),
    SlashCommand("fork", "/fork <ref>", "форкнуть сессию по id/префиксу"),
    SlashCommand("copy", "/copy", "скопировать последний ответ в буфер"),
    SlashCommand("quit", "/quit", "выход"),
)

_BY_NAME = {cmd.name: cmd for cmd in COMMANDS}
_BY_NAME["exit"] = _BY_NAME["quit"]  # алиас plain-REPL


def complete(prefix: str) -> list[SlashCommand]:
    """Команды, подходящие под ввод, начинающийся с '/' (для дропдауна)."""
    if not prefix.startswith("/"):
        return []
    head = prefix[1:].split(" ", 1)[0].lower()
    return [cmd for cmd in COMMANDS if cmd.name.startswith(head)]


def parse(line: str) -> tuple[SlashCommand | None, str] | None:
    """Разобрать ввод: (команда, аргументы) для '/…', None — обычное сообщение.

    Неизвестная слэш-команда возвращается как (None, имя) — фронтенд покажет
    подсказку вместо отправки '/опечатки' агенту.
    """
    line = line.strip()
    if not line.startswith("/"):
        return None
    head, _, tail = line[1:].partition(" ")
    return _BY_NAME.get(head.lower()), tail.strip() if head else ""
