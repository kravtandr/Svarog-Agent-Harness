"""Self-документация Svarog как reverse-tool на bridge.

Транспорт — MCP (`read_svarog_docs`), а не файлы в контейнере: `read` OpenCode
жёстко отвергает пути вне cwd («filePath resolves outside the working
directory»), поэтому ro-mount доков был для него нечитаем. MCP есть у
claude-code и opencode — обоих развёртываемых executor'ов.

Спека: docs/superpowers/specs/2026-07-22-self-docs-design.md
"""

from pathlib import Path

import pytest

from svarog_harness.runtime.agents.claude_code import ClaudeCodeAdapter
from svarog_harness.runtime.agents.codex import CodexAdapter
from svarog_harness.runtime.agents.opencode import OpencodeAdapter
from svarog_harness.runtime.self_docs import (
    build_docs_index,
    read_doc,
    resolve_docs_root,
    self_docs_hint,
)
from svarog_harness.tools.docs_tools import ReadSvarogDocsTool


def _fake_root(tmp_path: Path, *, with_agents: bool = True) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "README.md").write_text("# Svarog\n\nкоманды\n", encoding="utf-8")
    if with_agents:
        (root / "AGENTS.md").write_text("# правила\n", encoding="utf-8")
    (root / "docs" / "adr" / "0003-flows.md").write_text(
        "\n# ADR-0003. Три Git-flow\n\nпамять, скиллы, рабочий код\n", encoding="utf-8"
    )
    return root


# --- резолвер и индекс -------------------------------------------------------


def test_resolve_docs_root_finds_repo() -> None:
    root = resolve_docs_root()
    assert root is not None
    assert (root / "README.md").is_file()
    assert (root / "docs" / "adr").is_dir()


def test_index_lists_docs_with_adr_titles(tmp_path: Path) -> None:
    index = build_docs_index(root=_fake_root(tmp_path))
    assert "README.md" in index
    assert "AGENTS.md" in index
    assert "adr/0003-flows.md" in index
    assert "ADR-0003. Три Git-flow" in index


def test_index_omits_missing_agents(tmp_path: Path) -> None:
    index = build_docs_index(root=_fake_root(tmp_path, with_agents=False))
    assert "AGENTS.md" not in index


# --- чтение документа --------------------------------------------------------


def test_read_doc_returns_content(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    assert "команды" in read_doc("README.md", root=root)
    assert "память, скиллы, рабочий код" in read_doc("adr/0003-flows.md", root=root)


@pytest.mark.parametrize(
    "bad",
    [
        "../../../etc/passwd",
        "adr/../../README.md",
        "adr/sub/nested.md",
        "svarog.yaml",
        "adr/0003-flows.txt",
    ],
)
def test_read_doc_rejects_paths_outside_allowlist(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        read_doc(bad, root=_fake_root(tmp_path))


def test_read_doc_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        read_doc("adr/9999-nope.md", root=_fake_root(tmp_path))


# --- tool --------------------------------------------------------------------


async def test_tool_without_path_returns_index(tmp_path: Path) -> None:
    tool = ReadSvarogDocsTool(root=_fake_root(tmp_path))
    result = await tool.call({})
    assert result.ok
    assert "adr/0003-flows.md" in result.output


async def test_tool_reads_named_doc(tmp_path: Path) -> None:
    tool = ReadSvarogDocsTool(root=_fake_root(tmp_path))
    result = await tool.call({"path": "adr/0003-flows.md"})
    assert result.ok
    assert "память, скиллы, рабочий код" in result.output


async def test_tool_rejects_bad_path(tmp_path: Path) -> None:
    tool = ReadSvarogDocsTool(root=_fake_root(tmp_path))
    result = await tool.call({"path": "../../etc/passwd"})
    assert not result.ok


# --- указатель в контекст-файлах адаптеров -----------------------------------

# codex не имеет mcp — указателя быть не должно даже при self_docs=True.
_MCP_ADAPTERS = [
    (ClaudeCodeAdapter, "CLAUDE.md", "mcp__svarog__read_svarog_docs"),
    (OpencodeAdapter, ".config/opencode/AGENTS.md", "svarog_read_svarog_docs"),
]


@pytest.mark.parametrize("adapter_cls, ctx_file, tool_name", _MCP_ADAPTERS)
def test_context_files_name_adapter_specific_tool(adapter_cls, ctx_file, tool_name) -> None:
    files = adapter_cls().context_files("mem", "", True)
    assert tool_name in files[ctx_file]


@pytest.mark.parametrize("adapter_cls, ctx_file, tool_name", _MCP_ADAPTERS)
def test_context_files_omit_hint_when_disabled(adapter_cls, ctx_file, tool_name) -> None:
    files = adapter_cls().context_files("mem", "")
    assert "read_svarog_docs" not in files.get(ctx_file, "")


def test_codex_never_gets_hint() -> None:
    # mcp=False — tool недоступен; указатель был бы ложью (как ask_user-преамбула).
    files = CodexAdapter().context_files("mem", "", True)
    assert "read_svarog_docs" not in files.get("AGENTS.md", "")


def test_hint_names_tool() -> None:
    hint = self_docs_hint("mcp__svarog__read_svarog_docs")
    assert "mcp__svarog__read_svarog_docs" in hint
    assert "Svarog" in hint
