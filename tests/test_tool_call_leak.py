"""Тесты fallback-парсера протёкших tool call'ов (Harmony, Hermes/Qwen, Mistral)."""

from svarog_harness.llm.tool_call_leak import extract_leaked_tool_calls, leak_suspected

# Реальный случай: сервер отдал Harmony-каналы обычным текстом.
_LEAKED = (
    "analysisThe user wants to store a memory entry.assistant"
    "commentary to=functions.remember json{\n"
    '  "file": "user/profile.md",\n'
    '  "operation": "append",\n'
    '  "content": "## Дейлики\\n- вторник и четверг в 13:00"\n'
    "}assistantfinalЗапомнил: информация сохранена."
)


def test_extracts_call_from_leaked_channels() -> None:
    calls = extract_leaked_tool_calls(_LEAKED)
    assert len(calls) == 1
    call = calls[0]
    assert call.name == "remember"
    args = call.parse_arguments()
    assert args["file"] == "user/profile.md"
    assert args["operation"] == "append"


def test_extracts_multiple_calls() -> None:
    text = 'to=functions.read_file json{"path": "a.txt"} мусор to=functions.list_dir {"path": "."}'
    calls = extract_leaked_tool_calls(text)
    assert [c.name for c in calls] == ["read_file", "list_dir"]
    # id уникальны: approval сопоставляется по call_id в рамках run.
    assert all(c.id.startswith("leaked-") for c in calls)
    assert len({c.id for c in calls}) == 2


def test_handles_nested_braces_and_strings() -> None:
    text = (
        'to=functions.write_file json{"path": "x", "content": "def f():\\n    return {\\"a\\": 1}"}'
    )
    calls = extract_leaked_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].parse_arguments()["path"] == "x"


def test_skips_invalid_json_but_stays_suspected() -> None:
    text = "commentary to=functions.remember json{'file': 'user/profile.md',}"
    assert extract_leaked_tool_calls(text) == ()
    assert leak_suspected(text)


def test_skips_marker_without_arguments() -> None:
    text = "упоминание to=functions.remember без аргументов и всё"
    assert extract_leaked_tool_calls(text) == ()
    assert leak_suspected(text)


def test_skips_non_filler_gap() -> None:
    # '{' слишком далеко и через осмысленный текст — не наш вызов.
    text = 'to=functions.remember вот такой текст, а потом где-то {"file": "x"}'
    assert extract_leaked_tool_calls(text) == ()


def test_plain_text_not_suspected() -> None:
    text = "Обычный финальный ответ без каких-либо вызовов."
    assert not leak_suspected(text)
    assert extract_leaked_tool_calls(text) == ()


def test_extracts_hermes_qwen_dialect() -> None:
    text = (
        "Сейчас прочитаю файл.\n<tool_call>\n"
        '{"name": "read_file", "arguments": {"path": "a.txt"}}\n'
        "</tool_call>"
    )
    calls = extract_leaked_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].name == "read_file"
    assert calls[0].parse_arguments() == {"path": "a.txt"}
    assert leak_suspected(text)


def test_hermes_arguments_as_string() -> None:
    text = '<tool_call>{"name": "list_dir", "arguments": "{\\"path\\": \\".\\"}"}</tool_call>'
    calls = extract_leaked_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].parse_arguments() == {"path": "."}


def test_extracts_mistral_dialect() -> None:
    text = (
        '[TOOL_CALLS][{"name": "read_file", "arguments": {"path": "a.txt"}}, '
        '{"name": "list_dir", "arguments": {"path": "."}}]'
    )
    calls = extract_leaked_tool_calls(text)
    assert [c.name for c in calls] == ["read_file", "list_dir"]
    assert calls[1].parse_arguments() == {"path": "."}
    assert leak_suspected(text)


def test_broken_hermes_stays_suspected() -> None:
    text = '<tool_call>{"name": "read_file", "arguments": {broken</tool_call>'
    assert extract_leaked_tool_calls(text) == ()
    assert leak_suspected(text)
