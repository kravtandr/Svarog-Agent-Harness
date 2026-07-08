"""Тесты файловых tools: операции и запрет выхода за пределы workspace."""

from pathlib import Path

import pytest

from svarog_harness.tools.file_tools import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
    file_tools,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 42\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Проект\n", encoding="utf-8")
    return tmp_path


async def test_read_file(workspace: Path) -> None:
    result = await ReadFileTool(workspace).call({"path": "src/app.py"})
    assert result.ok
    assert "return 42" in result.output


async def test_read_missing_file(workspace: Path) -> None:
    result = await ReadFileTool(workspace).call({"path": "nope.txt"})
    assert not result.ok
    assert result.error is not None
    assert "не найден" in result.error


async def test_write_file_creates_parents(workspace: Path) -> None:
    result = await WriteFileTool(workspace).call({"path": "deep/dir/new.txt", "content": "данные"})
    assert result.ok
    assert (workspace / "deep/dir/new.txt").read_text(encoding="utf-8") == "данные"


async def test_edit_file_single_occurrence(workspace: Path) -> None:
    result = await EditFileTool(workspace).call(
        {"path": "src/app.py", "old_string": "return 42", "new_string": "return 7"}
    )
    assert result.ok
    assert "return 7" in (workspace / "src/app.py").read_text(encoding="utf-8")


async def test_edit_file_ambiguous_without_replace_all(workspace: Path) -> None:
    (workspace / "dup.txt").write_text("x\nx\n", encoding="utf-8")
    tool = EditFileTool(workspace)
    result = await tool.call({"path": "dup.txt", "old_string": "x", "new_string": "y"})
    assert not result.ok
    assert result.error is not None
    assert "2 раз" in result.error

    result = await tool.call(
        {"path": "dup.txt", "old_string": "x", "new_string": "y", "replace_all": True}
    )
    assert result.ok
    assert (workspace / "dup.txt").read_text(encoding="utf-8") == "y\ny\n"


async def test_edit_file_old_string_missing(workspace: Path) -> None:
    result = await EditFileTool(workspace).call(
        {"path": "src/app.py", "old_string": "нет такого", "new_string": "y"}
    )
    assert not result.ok
    assert result.error is not None
    assert "не найден" in result.error


async def test_list_dir_marks_directories(workspace: Path) -> None:
    result = await ListDirTool(workspace).call({})
    assert result.ok
    assert result.output.splitlines() == ["src/", "README.md"]


async def test_search_files(workspace: Path) -> None:
    result = await SearchFilesTool(workspace).call({"pattern": r"return \d+", "glob": "**/*.py"})
    assert result.ok
    assert result.output == "src/app.py:2: return 42"


async def test_search_files_no_matches(workspace: Path) -> None:
    result = await SearchFilesTool(workspace).call({"pattern": "абракадабра"})
    assert result.ok
    assert "не найдено" in result.output


async def test_search_files_invalid_regex(workspace: Path) -> None:
    result = await SearchFilesTool(workspace).call({"pattern": "("})
    assert not result.ok
    assert result.error is not None
    assert "регулярное" in result.error


@pytest.mark.parametrize("path", ["../outside.txt", "src/../../outside.txt"])
async def test_relative_escape_rejected(workspace: Path, path: str) -> None:
    for tool in (ReadFileTool(workspace), WriteFileTool(workspace)):
        args = {"path": path} if tool.name == "read_file" else {"path": path, "content": "x"}
        result = await tool.call(args)
        assert not result.ok
        assert result.error is not None
        assert "за пределы workspace" in result.error


async def test_absolute_path_rejected(workspace: Path) -> None:
    result = await ReadFileTool(workspace).call({"path": str(workspace / "README.md")})
    assert not result.ok
    assert result.error is not None
    assert "абсолютные пути запрещены" in result.error


async def test_symlink_escape_rejected(
    workspace: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("секрет", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)
    result = await ReadFileTool(workspace).call({"path": "link.txt"})
    assert not result.ok
    assert result.error is not None
    assert "за пределы workspace" in result.error


def test_file_tools_factory(workspace: Path) -> None:
    names = {tool.name for tool in file_tools(workspace)}
    assert names == {"read_file", "write_file", "edit_file", "list_dir", "search_files"}
