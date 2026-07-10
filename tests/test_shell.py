"""Тесты bash tool поверх LocalEnvironment: exit code, потоки, timeout."""

import time
from pathlib import Path

from svarog_harness.sandbox.local import LocalEnvironment
from svarog_harness.tools.shell import BashTool


def _tool(workspace: Path, command_timeout_sec: float = 120.0) -> BashTool:
    return BashTool(LocalEnvironment(workspace), command_timeout_sec)


async def test_captures_stdout_and_exit_code(tmp_path: Path) -> None:
    result = await _tool(tmp_path).call({"command": "echo hello"})
    assert result.ok
    assert "exit code: 0" in result.output
    assert "hello" in result.output


async def test_runs_in_workspace_cwd(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("здесь", encoding="utf-8")
    result = await _tool(tmp_path).call({"command": "ls"})
    assert result.ok
    assert "marker.txt" in result.output


async def test_nonzero_exit_reported_as_failure(tmp_path: Path) -> None:
    result = await _tool(tmp_path).call({"command": "echo oops >&2; exit 3"})
    assert not result.ok
    assert result.error == "exit code 3"
    assert "stderr:" in result.output
    assert "oops" in result.output


async def test_timeout_kills_process_group(tmp_path: Path) -> None:
    tool = _tool(tmp_path, command_timeout_sec=0.2)
    start = time.monotonic()
    result = await tool.call({"command": "sleep 30"})
    elapsed = time.monotonic() - start
    assert elapsed < 5
    assert not result.ok
    assert result.error is not None
    assert "timeout" in result.error


async def test_output_captured_fully_below_capture_cap(tmp_path: Path) -> None:
    """ADR-0015 §1.2: backpressure в loop — tool возвращает вывод целиком."""
    result = await _tool(tmp_path).call({"command": "head -c 100000 /dev/zero | tr '\\0' 'a'"})
    assert result.ok
    assert "вывод обрезан" not in result.output
    assert result.output.count("a") == 100_000


async def test_output_truncated_at_capture_cap(tmp_path: Path) -> None:
    """Потолок захвата ~1 МБ остаётся как защита памяти процесса."""
    result = await _tool(tmp_path).call({"command": "head -c 1500000 /dev/zero | tr '\\0' 'a'"})
    assert result.ok
    assert "вывод обрезан" in result.output
    assert len(result.output) < 1_100_000
