"""Тесты CLI: svarog run и svarog traces list/show на временной БД."""

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from svarog_harness.cli import main as cli_main
from svarog_harness.config.schema import ModelsConfig
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.runtime import orchestrator

runner = CliRunner()


class ScriptedProvider(ModelProvider):
    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        result = self.turns.pop(0)
        if on_text_delta is not None and result.content:
            on_text_delta(result.content)
        return result


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    db_path = tmp_path / "state" / "svarog.db"
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {db_path}\n",
        encoding="utf-8",
    )
    # traces-команды читают конфиг из cwd.
    monkeypatch.chdir(ws)
    # Пользовательский конфиг с хоста (~/.svarog/svarog.yaml) не должен влиять на тест.
    monkeypatch.setenv("HOME", str(tmp_path))
    # Широкий терминал, чтобы Rich-таблицы не обрезали ячейки в assert'ах.
    monkeypatch.setenv("COLUMNS", "200")
    return ws


def _patch_provider(monkeypatch: pytest.MonkeyPatch, turns: list[CompletionResult]) -> None:
    provider = ScriptedProvider(turns)

    def fake_default_provider(models_cfg: ModelsConfig, store: object = None) -> ModelProvider:
        return provider

    # Оркестрация прогона (и с ней default_provider) вынесена в runtime.orchestrator;
    # CLI лишь делегирует TaskRunner'у (см. рефакторинг M5).
    monkeypatch.setattr(orchestrator, "default_provider", fake_default_provider)


def test_run_completes_and_prints_answer(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_provider(
        monkeypatch,
        [CompletionResult(content="Всё сделано", usage=Usage(10, 5), finish_reason="stop")],
    )
    result = runner.invoke(cli_main.app, ["run", "простая задача", "--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    assert "Всё сделано" in result.output
    assert "completed" in result.output


def test_run_with_tool_call_touches_workspace(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(
        monkeypatch,
        [
            CompletionResult(
                content="",
                tool_calls=(
                    ToolCallRequest(
                        id="c1",
                        name="write_file",
                        arguments_json='{"path": "out.txt", "content": "готово"}',
                    ),
                ),
                usage=Usage(10, 5),
            ),
            CompletionResult(content="Файл создан", usage=Usage(10, 5), finish_reason="stop"),
        ],
    )
    result = runner.invoke(cli_main.app, ["run", "создай файл", "--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    assert (workspace / "out.txt").read_text(encoding="utf-8") == "готово"


def test_verifier_failure_blocks_success_and_autocommit(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    (workspace / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=workspace, check=True, capture_output=True)

    with (workspace / "svarog.yaml").open("a", encoding="utf-8") as config:
        config.write(
            "verifier:\n"
            "  checks:\n"
            "    - name: must-fail\n"
            "      command: test -f definitely-missing.txt\n"
        )
    _patch_provider(
        monkeypatch,
        [
            CompletionResult(
                content="",
                tool_calls=(
                    ToolCallRequest(
                        id="c1",
                        name="write_file",
                        arguments_json='{"path": "out.txt", "content": "готово"}',
                    ),
                ),
                usage=Usage(10, 5),
            ),
            CompletionResult(content="Файл создан", usage=Usage(10, 5), finish_reason="stop"),
        ],
    )

    result = runner.invoke(cli_main.app, ["run", "создай файл", "--workspace", str(workspace)])
    assert result.exit_code == 4, result.output
    assert "verifier" in result.output
    assert (workspace / "out.txt").read_text(encoding="utf-8") == "готово"
    committed_files = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "out.txt" not in committed_files


def test_run_iteration_limit_suspends_with_exit_code_3(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endless = [
        CompletionResult(
            content="",
            tool_calls=(ToolCallRequest(id=f"c{i}", name="list_dir", arguments_json="{}"),),
            usage=Usage(10, 5),
        )
        for i in range(60)
    ]
    _patch_provider(monkeypatch, endless)
    # Отключаем refuel (порог > max), чтобы проверить именно стоп-кран max_iterations.
    monkeypatch.setenv("SVAROG_RUNTIME__REFUEL_AFTER_ITERATIONS", "100")
    result = runner.invoke(cli_main.app, ["run", "зациклись", "--workspace", str(workspace)])
    assert result.exit_code == 3
    assert "лимит итераций" in result.output
    assert "svarog resume" in result.output


def test_resume_continues_suspended_run(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_provider(
        monkeypatch,
        [
            CompletionResult(
                content="",
                tool_calls=(ToolCallRequest(id=f"c{i}", name="list_dir", arguments_json="{}"),),
                usage=Usage(10, 5),
            )
            for i in range(2)
        ]
        + [CompletionResult(content="закончил после resume", usage=Usage(10, 5))],
    )
    # Низкий порог refuel → run приостанавливается (refuel-suspend, ADR-0005);
    # resume поднимает задачу и доводит до конца.
    monkeypatch.setenv("SVAROG_RUNTIME__REFUEL_AFTER_ITERATIONS", "1")
    monkeypatch.setenv("SVAROG_RUNTIME__MAX_ITERATIONS", "2")
    result = runner.invoke(cli_main.app, ["run", "длинная", "--workspace", str(workspace)])
    assert result.exit_code == 3, result.output

    run_id = result.output.rsplit("svarog resume ", 1)[1].split()[0]
    monkeypatch.delenv("SVAROG_RUNTIME__MAX_ITERATIONS")
    monkeypatch.delenv("SVAROG_RUNTIME__REFUEL_AFTER_ITERATIONS")
    resumed = runner.invoke(cli_main.app, ["resume", run_id])
    assert resumed.exit_code == 0, resumed.output
    assert "закончил после resume" in resumed.output
    assert "completed" in resumed.output


def test_resume_rejects_changed_security_config(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproducer 0.4 (ADR-0015): подмена провайдера между стартом и resume →
    fail-closed, resume отклоняется, а не исполняется под новым конфигом."""
    _patch_provider(
        monkeypatch,
        [
            CompletionResult(
                content="",
                tool_calls=(ToolCallRequest(id=f"c{i}", name="list_dir", arguments_json="{}"),),
                usage=Usage(10, 5),
            )
            for i in range(2)
        ]
        + [CompletionResult(content="не должно дойти", usage=Usage(10, 5))],
    )
    monkeypatch.setenv("SVAROG_RUNTIME__REFUEL_AFTER_ITERATIONS", "1")
    monkeypatch.setenv("SVAROG_RUNTIME__MAX_ITERATIONS", "2")
    result = runner.invoke(cli_main.app, ["run", "длинная", "--workspace", str(workspace)])
    assert result.exit_code == 3, result.output
    run_id = result.output.rsplit("svarog resume ", 1)[1].split()[0]
    monkeypatch.delenv("SVAROG_RUNTIME__MAX_ITERATIONS")
    monkeypatch.delenv("SVAROG_RUNTIME__REFUEL_AFTER_ITERATIONS")

    # Подменяем endpoint провайдера в конфиге workspace'а после старта run.
    cfg_path = workspace / "svarog.yaml"
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8").replace(
            "http://localhost:9/v1", "http://evil.example/v1"
        ),
        encoding="utf-8",
    )
    resumed = runner.invoke(cli_main.app, ["resume", run_id])
    assert resumed.exit_code == 1, resumed.output
    assert "security-конфиг" in resumed.output
    assert "не должно дойти" not in resumed.output


def test_approval_flow_via_cli(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run → waiting_approval → approvals list/approve → resume → completed."""
    _patch_provider(
        monkeypatch,
        [
            CompletionResult(
                content="",
                tool_calls=(
                    ToolCallRequest(
                        id="c1",
                        name="request_approval",
                        arguments_json='{"action": "рискованный шаг", "details": "rm -rf build"}',
                    ),
                ),
                usage=Usage(10, 5),
            ),
            CompletionResult(content="одобрено, продолжаю", usage=Usage(10, 5)),
        ],
    )
    result = runner.invoke(cli_main.app, ["run", "рискованная", "--workspace", str(workspace)])
    assert result.exit_code == 3, result.output
    assert "waiting_approval" in result.output
    run_id = result.output.rsplit("svarog resume ", 1)[1].split()[0]

    list_result = runner.invoke(cli_main.app, ["approvals", "list"])
    assert list_result.exit_code == 0, list_result.output
    assert "рискованный шаг" in list_result.output
    approval_id = list_result.output.split("approvals approve/deny ", 1)[1].split()[0]

    approve_result = runner.invoke(cli_main.app, ["approvals", "approve", approval_id])
    assert approve_result.exit_code == 0, approve_result.output
    assert "одобрен" in approve_result.output

    resumed = runner.invoke(cli_main.app, ["resume", run_id])
    assert resumed.exit_code == 0, resumed.output
    assert "одобрено, продолжаю" in resumed.output


def test_approvals_list_empty(workspace: Path) -> None:
    result = runner.invoke(cli_main.app, ["approvals", "list"])
    assert result.exit_code == 0
    assert "ожидающих approvals нет" in result.output


def test_resume_unknown_run(workspace: Path) -> None:
    result = runner.invoke(cli_main.app, ["resume", "deadbeef"])
    assert result.exit_code == 1
    assert "не найден" in result.output


def test_run_rejects_conflicting_autonomy_flags(workspace: Path) -> None:
    result = runner.invoke(
        cli_main.app,
        ["run", "задача", "--workspace", str(workspace), "--yolo", "--supervised"],
    )
    assert result.exit_code == 1
    assert "взаимоисключающие" in result.output


def test_run_missing_workspace_fails(workspace: Path) -> None:
    result = runner.invoke(cli_main.app, ["run", "задача", "--workspace", "/nonexistent/dir"])
    assert result.exit_code == 1
    assert "workspace не существует" in result.output


def test_traces_list_and_show(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_provider(
        monkeypatch,
        [CompletionResult(content="Ответ", usage=Usage(10, 5), finish_reason="stop")],
    )
    run_result = runner.invoke(cli_main.app, ["run", "трейс42", "--workspace", str(workspace)])
    assert run_result.exit_code == 0, run_result.output

    list_result = runner.invoke(cli_main.app, ["traces", "list"])
    assert list_result.exit_code == 0, list_result.output
    # Rich-таблица переносит текст по словам — сравниваем нормализованный вывод.
    normalized = " ".join(list_result.output.split())
    assert "трейс42" in normalized
    assert "completed" in normalized

    run_id = None
    for line in run_result.output.splitlines():
        if "run " in line and "итераций" in line:
            run_id = line.split("run ")[1].split(" ")[0]
    assert run_id, run_result.output

    show_result = runner.invoke(cli_main.app, ["traces", "show", run_id])
    assert show_result.exit_code == 0, show_result.output
    assert "трейс42" in show_result.output
    assert "assistant" in show_result.output


def test_traces_show_unknown_run(workspace: Path) -> None:
    result = runner.invoke(cli_main.app, ["traces", "show", "deadbeef"])
    assert result.exit_code == 1
    assert "не найден" in result.output


def test_traces_list_empty_db(workspace: Path) -> None:
    result = runner.invoke(cli_main.app, ["traces", "list"])
    assert result.exit_code == 0
    assert "runs пока нет" in result.output
