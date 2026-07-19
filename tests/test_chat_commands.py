"""Слэш-команды inline-чата: парсинг."""

from svarog_harness.cli.chat_commands import parse


def test_parse_plain_message_is_none() -> None:
    assert parse("обычное сообщение") is None


def test_parse_known_command_with_args() -> None:
    parsed = parse("/fork abc123")
    assert parsed is not None
    command, args = parsed
    assert command is not None and command.name == "fork"
    assert args == "abc123"


def test_parse_exit_alias_maps_to_quit() -> None:
    parsed = parse("/exit")
    assert parsed is not None
    command, _ = parsed
    assert command is not None and command.name == "quit"


def test_parse_unknown_command_returns_none_command() -> None:
    parsed = parse("/опечатка")
    assert parsed is not None
    command, _ = parsed
    assert command is None
