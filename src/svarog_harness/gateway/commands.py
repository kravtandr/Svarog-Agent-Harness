"""Слэш-команды веб-чата.

Реестр свой, а не CLI-шный: `/quit` и `/mode` в браузере бессмысленны, а
`/fork` требует серверной поддержки форка сессии, которой пока нет.
Дедупликация с CLI не нужна — пересекается только тип SlashCommand.
"""

from svarog_harness.cli.chat_commands import SlashCommand

WEB_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("help", "/help", "показать команды"),
    SlashCommand("new", "/new", "новый чат"),
    SlashCommand("sessions", "/sessions", "перейти к списку чатов"),
    SlashCommand("executor", "/executor", "выбрать исполнителя"),
    SlashCommand("policies", "/policies", "выбрать автономию"),
    SlashCommand("copy", "/copy", "скопировать последний ответ"),
)
