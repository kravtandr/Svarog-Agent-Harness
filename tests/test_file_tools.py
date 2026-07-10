"""Тесты файловых tools: операции и запрет выхода за пределы workspace."""

import shutil
import subprocess
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
        assert "за пределы" in result.error


async def test_absolute_path_rejected(workspace: Path) -> None:
    result = await ReadFileTool(workspace).call({"path": str(workspace / "README.md")})
    assert not result.ok
    assert result.error is not None
    assert "абсолютный путь запрещён" in result.error


async def test_symlink_escape_rejected(
    workspace: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("секрет", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)
    result = await ReadFileTool(workspace).call({"path": "link.txt"})
    assert not result.ok
    assert result.error is not None
    assert "за пределы" in result.error


def test_file_tools_factory(workspace: Path) -> None:
    names = {tool.name for tool in file_tools(workspace)}
    assert names == {"read_file", "write_file", "edit_file", "list_dir", "search_files"}


# --- 0.2 (ADR-0015): запрет записи в управляющее дерево .git/.svarog ----------


@pytest.mark.parametrize(
    "path",
    [".git/hooks/pre-commit", ".git/config", ".svarog/svarog.db", ".svarog/tool-results/x.txt"],
)
async def test_write_into_control_tree_rejected(workspace: Path, path: str) -> None:
    (workspace / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
    result = await WriteFileTool(workspace).call({"path": path, "content": "#!/bin/sh\nid\n"})
    assert not result.ok
    assert result.error is not None
    assert "управляющий каталог" in result.error
    # host-git hook НЕ посажен: запись отвергнута до касания диска.
    assert not (workspace / path).exists()


async def test_edit_into_control_tree_rejected(workspace: Path) -> None:
    hook = workspace / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\ntrue\n", encoding="utf-8")
    result = await EditFileTool(workspace).call(
        {"path": ".git/hooks/pre-commit", "old_string": "true", "new_string": "id"}
    )
    assert not result.ok
    assert result.error is not None
    assert "управляющий каталог" in result.error
    assert "true" in hook.read_text(encoding="utf-8")  # не изменён


async def test_read_from_control_tree_allowed(workspace: Path) -> None:
    """Чтение из .svarog не запрещено — spill-файлы (1.2) достаются read_file."""
    spill = workspace / ".svarog" / "tool-results" / "run" / "call.txt"
    spill.parent.mkdir(parents=True, exist_ok=True)
    spill.write_text("полный вывод", encoding="utf-8")
    result = await ReadFileTool(workspace).call({"path": ".svarog/tool-results/run/call.txt"})
    assert result.ok
    assert "полный вывод" in result.output


def test_read_only_tools_declare_execution_metadata(tmp_path: Path) -> None:
    """ADR-0015 §1.1: читающие file-tools параллелятся, пишущие — нет."""
    from svarog_harness.tools.file_tools import (
        EditFileArgs,
        EditFileTool,
        ListDirArgs,
        ListDirTool,
        ReadFileArgs,
        SearchFilesArgs,
        SearchFilesTool,
        WriteFileArgs,
    )

    assert ReadFileTool(tmp_path).is_read_only(ReadFileArgs(path="a.txt")) is True
    assert ListDirTool(tmp_path).is_read_only(ListDirArgs(path=".")) is True
    assert SearchFilesTool(tmp_path).is_read_only(SearchFilesArgs(pattern="x")) is True
    assert WriteFileTool(tmp_path).is_read_only(WriteFileArgs(path="a", content="b")) is False
    assert (
        EditFileTool(tmp_path).is_read_only(EditFileArgs(path="a", old_string="x", new_string="y"))
        is False
    )


async def test_read_file_offset_and_limit(workspace: Path) -> None:
    """ADR-0015 §1.2/§4: дочитывание файла частями через offset/limit."""
    lines = "\n".join(f"строка {i}" for i in range(1, 11))
    (workspace / "long.txt").write_text(lines, encoding="utf-8")

    result = await ReadFileTool(workspace).call({"path": "long.txt", "offset": 3, "limit": 2})
    assert result.ok
    assert "строка 3" in result.output
    assert "строка 4" in result.output
    assert "строка 5" not in result.output
    # Маркер объясняет, что показано и как дочитать.
    assert "строки 3–4 из 10" in result.output
    assert "offset=5" in result.output


async def test_read_file_offset_past_end(workspace: Path) -> None:
    (workspace / "short.txt").write_text("одна строка", encoding="utf-8")
    result = await ReadFileTool(workspace).call({"path": "short.txt", "offset": 5})
    assert not result.ok
    assert result.error is not None
    assert "за концом файла" in result.error


async def test_read_file_whole_file_has_no_marker(workspace: Path) -> None:
    (workspace / "small.txt").write_text("a\nb", encoding="utf-8")
    result = await ReadFileTool(workspace).call({"path": "small.txt"})
    assert result.ok
    assert result.output == "a\nb"


# --- ADR-0015 фаза 4: rg-backed search_files ---------------------------------


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep недоступен")
async def test_search_respects_gitignore_with_rg(tmp_path: Path) -> None:
    """rg-backend уважает .gitignore (корректность игнора — суть фазы 4)."""
    ws = tmp_path / "repo"
    ws.mkdir()
    subprocess.run(["git", "-C", str(ws), "init", "-q"], check=True)
    (ws / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (ws / "ignored.txt").write_text("иголка тут", encoding="utf-8")
    (ws / "visible.txt").write_text("иголка здесь", encoding="utf-8")

    result = await SearchFilesTool(ws).call({"pattern": "иголка"})
    assert result.ok
    assert "visible.txt" in result.output
    assert "ignored.txt" not in result.output


async def test_search_python_fallback_without_rg(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без ripgrep поиск деградирует до Python-обхода с тем же контрактом."""
    from svarog_harness.tools import file_tools as ft

    monkeypatch.setattr(ft.shutil, "which", lambda name: None)
    result = await SearchFilesTool(workspace).call({"pattern": r"return \d+", "glob": "**/*.py"})
    assert result.ok
    assert result.output == "src/app.py:2: return 42"


async def test_search_max_results_honest_marker(tmp_path: Path) -> None:
    ws = tmp_path / "many"
    ws.mkdir()
    (ws / "data.txt").write_text("\n".join("иголка" for _ in range(10)), encoding="utf-8")

    result = await SearchFilesTool(ws).call({"pattern": "иголка", "max_results": 3})
    assert result.ok
    shown = [line for line in result.output.splitlines() if line.startswith("data.txt:")]
    assert len(shown) == 3
    assert "показано 3" in result.output
    assert "10" in result.output  # честный итог: сколько совпадений всего


async def test_search_rust_incompatible_regex_falls_back(tmp_path: Path) -> None:
    """Паттерн с backreference не знаком rust-regex — работает Python-fallback."""
    ws = tmp_path / "bref"
    ws.mkdir()
    (ws / "x.txt").write_text("шаблон: aa", encoding="utf-8")

    result = await SearchFilesTool(ws).call({"pattern": r"(a)\1"})
    assert result.ok
    assert "x.txt:1:" in result.output
