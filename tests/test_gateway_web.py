"""Тесты веб-доработок gateway: список сессий, лента, статика (план 2026-07-27)."""

import asyncio
import contextlib
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app
from svarog_harness.gateway.service import _RunHolder
from svarog_harness.runtime.summaries import short_arg, short_result


def _write_config(ws: Path, tmp_path: Path) -> None:
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


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayService:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    return GatewayService(load_config(project_dir=ws), ws)


@pytest.mark.asyncio
async def test_list_sessions_newest_first(service: GatewayService) -> None:
    first = await service.create_session(title="старая")
    second = await service.create_session(title="свежая")

    listed = await service.list_sessions()

    assert [s.title for s in listed] == ["свежая", "старая"]
    assert [s.session_id for s in listed] == [second.session_id, first.session_id]
    assert listed[0].runs_count == 0
    assert listed[0].last_state is None
    assert isinstance(listed[0].updated_at, datetime)


@pytest.mark.asyncio
async def test_list_sessions_endpoint(service: GatewayService) -> None:
    await service.create_session(title="через API")
    client = TestClient(create_app(service=service))

    response = client.get("/sessions")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["title"] == "через API"
    assert body[0]["runs_count"] == 0


def test_sandbox_label_replaces_container_path() -> None:
    """Путь внутри контейнера человеку ничего не говорит.

    Агент в docker видит рабочую папку как /workspace — и в аргументе, и в
    выводе инструмента. В ленте это выглядело так, будто он работает не там
    (запрос 06.08.2026). Показываем метку песочницы и имя настоящей папки.
    """
    ws = Path("/Users/kto/proj/test")
    assert short_arg({"path": "/workspace"}, workspace=ws) == "<sandbox>/test"
    assert (
        short_arg({"path": "/workspace/src/main.py"}, workspace=ws) == "<sandbox>/test/src/main.py"
    )
    # `read` по каталогу возвращает путь в выводе — правая часть строки.
    assert (
        short_result(
            ok=True,
            output="<path>/workspace</path>\n<type>directory</type>",
            workspace=ws,
        )
        == "<path><sandbox>/test</path>"
    )


def test_sandbox_label_leaves_other_paths_alone() -> None:
    """Похожие по началу пути не трогаем: /workspaces — другая папка."""
    ws = Path("/Users/kto/proj/test")
    assert short_arg({"path": "/workspaces/чужое"}, workspace=ws) == "/workspaces/чужое"
    # Без workspace (не sandbox-прогон) значение остаётся как есть.
    assert short_arg({"path": "/workspace/a"}) == "/workspace/a"


def test_short_arg_prefers_meaningful_key() -> None:
    assert short_arg({"path": "memory/index.py", "content": "x" * 500}) == "memory/index.py"
    assert short_arg({"command": "uv run pytest -q"}) == "uv run pytest -q"
    assert short_arg({"query": "стоп-слова префикс"}) == "стоп-слова префикс"
    assert short_arg({}) == ""


def test_short_arg_truncates_long_values() -> None:
    assert short_arg({"path": "a" * 200}) == "a" * 119 + "…"


def test_short_result_takes_first_line_of_output() -> None:
    assert (
        short_result(ok=True, output="записано 1234 символов в memory/index.py")
        == "записано 1234 символов в memory/index.py"
    )
    hits = "- a.md — фрагмент\n- b.md — фрагмент"
    assert short_result(ok=True, output=hits) == "- a.md — фрагмент"
    assert short_result(ok=True, output="\n\n  первая непустая  \nвторая") == "первая непустая"


def test_short_result_reports_failure_and_truncates() -> None:
    assert short_result(ok=False, output="", error="exit code 1: no such file") == (
        "exit code 1: no such file"
    )
    assert short_result(ok=False, output="", error=None) == "ошибка"
    assert short_result(ok=True, output="") == ""
    assert short_result(ok=True, output="я" * 100) == "я" * 59 + "…"


@pytest.mark.asyncio
async def test_tool_events_carry_arg_and_result(service: GatewayService) -> None:
    published: list[dict[str, object]] = []
    service.events.publish = lambda run_id, event: published.append(event)  # type: ignore[method-assign]

    started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    holder = _RunHolder()
    holder.run_id = "run-1"
    hooks = service._event_hooks(holder, started)

    assert hooks.on_tool_call is not None
    assert hooks.on_tool_result is not None
    hooks.on_tool_call("write_file", {"path": "memory/index.py", "content": "x"})
    hooks.on_tool_result("write_file", "succeeded", "+58 −4")

    assert published == [
        {"type": "tool_call", "tool": "write_file", "arg": "memory/index.py"},
        {"type": "tool_result", "tool": "write_file", "status": "succeeded", "result": "+58 −4"},
    ]


@pytest.mark.asyncio
async def test_progress_event_published(service: GatewayService) -> None:
    """on_progress → WS-событие progress: живой счётчик токенов в чате."""
    published: list[dict[str, object]] = []
    service.events.publish = lambda run_id, event: published.append(event)  # type: ignore[method-assign]

    started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    holder = _RunHolder()
    holder.run_id = "run-1"
    hooks = service._event_hooks(holder, started)

    assert hooks.on_progress is not None
    hooks.on_progress(3, 12_400, 0.04, 0.0, 0)

    assert published == [{"type": "progress", "iterations": 3, "tokens": 12_400, "cost_usd": 0.04}]


@pytest.mark.asyncio
async def test_session_thread_replays_user_calls_and_answer(service: GatewayService) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from svarog_harness.storage.models import Run, RunState, ToolCall, ToolCallStatus

    session = await service.create_session(title="лента")

    async def seed(db: AsyncSession) -> None:
        db.add(
            Run(
                id="run-1",
                session_id=session.session_id,
                task="Добавь FTS-поиск",
                state=RunState.COMPLETED,
                autonomy="supervised",
            )
        )
        db.add(
            ToolCall(
                id="tc-1",
                run_id="run-1",
                tool_name="write_file",
                arguments={"path": "memory/index.py"},
                result={"output": "записано 1234 символов в memory/index.py"},
                status=ToolCallStatus.SUCCEEDED,
            )
        )
        await db.commit()

    await service._read(seed)

    thread = await service.session_thread(session.session_id)

    assert thread.items[0].kind == "user"
    assert thread.items[0].text == "Добавь FTS-поиск"
    call = next(item for item in thread.items if item.kind == "call")
    assert call.name == "write_file"
    assert call.server is None
    assert call.arg == "memory/index.py"
    assert call.result == "записано 1234 символов в memory/index.py"
    assert call.status == "succeeded"


@pytest.mark.asyncio
async def test_session_thread_splits_mcp_server_from_tool_name(service: GatewayService) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from svarog_harness.storage.models import Run, RunState, ToolCall, ToolCallStatus

    session = await service.create_session(title="mcp")

    async def seed(db: AsyncSession) -> None:
        db.add(
            Run(
                id="run-2",
                session_id=session.session_id,
                task="Посмотри задачи",
                state=RunState.COMPLETED,
                autonomy="supervised",
            )
        )
        db.add(
            ToolCall(
                id="tc-2",
                run_id="run-2",
                tool_name="github/list_issues",
                arguments={"query": "label: memory"},
                result={"output": "2 задачи"},
                status=ToolCallStatus.SUCCEEDED,
            )
        )
        await db.commit()

    await service._read(seed)

    thread = await service.session_thread(session.session_id)

    call = next(item for item in thread.items if item.kind == "call")
    assert call.server == "github"
    assert call.name == "list_issues"


def test_session_thread_endpoint_404(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))
    assert client.get("/sessions/нет-такой/messages").status_code == 404


@pytest.fixture
def service_with_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayService:
    """Сервис с настроенной памятью: пара страниц на диске."""
    ws = tmp_path / "ws-mem"
    ws.mkdir()
    memory = tmp_path / "memory"
    (memory / "решения").mkdir(parents=True)
    (memory / "решения" / "fts.md").write_text(
        "# Retrieval\n\nТочный проход раньше широкого.\n", encoding="utf-8"
    )
    (memory / "профиль.md").write_text("# Профиль\n", encoding="utf-8")

    db_path = tmp_path / "state-mem" / "svarog.db"
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {db_path}\n"
        f"memory:\n  path: {memory}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    return GatewayService(load_config(project_dir=ws), ws)


def test_memory_tree_lists_markdown_pages(service_with_memory: GatewayService) -> None:
    pages = service_with_memory.memory_tree()

    assert [page.path for page in pages] == ["профиль.md", "решения/fts.md"]
    assert all(page.size_bytes > 0 for page in pages)


def test_memory_file_returns_text(service_with_memory: GatewayService) -> None:
    view = service_with_memory.memory_file("решения/fts.md")
    assert "Точный проход раньше широкого" in view.text


def test_memory_file_refuses_escape_and_non_markdown(
    service_with_memory: GatewayService,
) -> None:
    from svarog_harness.gateway.service import MemoryPathError

    with pytest.raises(MemoryPathError):
        service_with_memory.memory_file("../../etc/passwd")
    with pytest.raises(MemoryPathError):
        service_with_memory.memory_file("../svarog.yaml")
    with pytest.raises(MemoryPathError):
        service_with_memory.memory_file("решения/секрет.json")
    # Ключевой кейс: .md за пределами memory/. Без него тест проходил бы
    # даже при полностью снятой защите от обхода — отсекал бы суффикс.
    outside = service_with_memory.workspace.parent / "чужое.md"
    outside.write_text("не наше", encoding="utf-8")
    with pytest.raises(MemoryPathError):
        service_with_memory.memory_file("../../чужое.md")


def test_memory_endpoints_report_disabled_memory(service: GatewayService) -> None:
    # У базовой фикстуры memory.path не задан — раздела просто нет.
    client = TestClient(create_app(service=service))

    response = client.get("/memory/tree")

    assert response.status_code == 404
    assert "память не настроена" in response.json()["detail"]


def test_memory_endpoints(service_with_memory: GatewayService) -> None:
    client = TestClient(create_app(service=service_with_memory))

    tree = client.get("/memory/tree")
    assert tree.status_code == 200
    assert len(tree.json()) == 2

    file = client.get("/memory/file", params={"path": "решения/fts.md"})
    assert file.status_code == 200
    assert "Retrieval" in file.json()["text"]

    escape = client.get("/memory/file", params={"path": "../svarog.yaml"})
    assert escape.status_code == 422


@pytest.mark.asyncio
async def test_config_form_built_from_schema(service: GatewayService) -> None:
    view = service.describe_config()

    fields = {f.path: f for section in view.sections for f in section.fields}

    # Тип и набор значений выведены из схемы, а не заданы руками.
    assert fields["runtime.autonomy"].kind == "enum"
    assert fields["runtime.autonomy"].choices == ["supervised", "auto", "yolo"]
    assert fields["runtime.autonomy"].value == "yolo"  # дефолт RuntimeConfig
    # Ограничение gt=0 из Field попало в форму.
    assert fields["runtime.max_iterations"].kind == "int"
    assert fields["runtime.max_iterations"].minimum == 0
    assert fields["git.require_approval_for_push"].kind == "bool"
    assert fields["git.require_approval_for_push"].value is True
    assert fields["sandbox.type"].choices == ["docker", "local-trusted"]
    assert view.path.endswith("svarog.yaml")


@pytest.mark.asyncio
async def test_config_preview_shows_diff_without_writing(service: GatewayService) -> None:
    before = service.config_path.read_text(encoding="utf-8")

    view = service.preview_config({"runtime.autonomy": "supervised"})

    assert view.changes > 0
    added = [line.text for line in view.lines if line.kind == "add"]
    assert any("supervised" in text for text in added)
    # Файл не тронут: запись только по «Сохранить».
    assert service.config_path.read_text(encoding="utf-8") == before


def test_patch_yaml_keeps_comments_and_layout() -> None:
    """Правка одного ключа не должна переписывать файл, который ведут руками."""
    from svarog_harness.gateway.settings import patch_yaml_text

    before = (
        "# Конфигурация Svarog (§13).\n"
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: https://openrouter.ai/api   # vLLM, llama.cpp…\n"
        "\n"
        "runtime:\n"
        "  autonomy: yolo\n"
        "\n"
        "sandbox:\n"
        "  type: docker   # изоляция\n"
    )

    after = patch_yaml_text(before, {"runtime.autonomy": "supervised"})

    assert "# Конфигурация Svarog (§13)." in after
    assert "# vLLM, llama.cpp…" in after
    assert "  type: docker   # изоляция" in after
    assert "  autonomy: supervised" in after
    # Ровно одна строка отличается — всё остальное байт в байт.
    changed = [
        (a, b) for a, b in zip(before.splitlines(), after.splitlines(), strict=True) if a != b
    ]
    assert changed == [("  autonomy: yolo", "  autonomy: supervised")]


def test_patch_yaml_preserves_trailing_comment_of_edited_line() -> None:
    from svarog_harness.gateway.settings import patch_yaml_text

    after = patch_yaml_text(
        "sandbox:\n  type: docker   # изоляция\n", {"sandbox.type": "local-trusted"}
    )

    assert after == "sandbox:\n  type: local-trusted   # изоляция\n"


def test_patch_yaml_adds_missing_key_and_section() -> None:
    from svarog_harness.gateway.settings import patch_yaml_text

    after = patch_yaml_text("runtime:\n  autonomy: yolo\n", {"runtime.max_iterations": 7})
    assert "  max_iterations: 7" in after
    assert "  autonomy: yolo" in after

    fresh = patch_yaml_text("models:\n  default: local\n", {"git.auto_commit": False})
    assert fresh.endswith("git:\n  auto_commit: false\n")


@pytest.mark.asyncio
async def test_config_write_keeps_file_readable_by_human(service: GatewayService) -> None:
    """Сохранение из интерфейса не стирает комментарии реального файла."""
    service.config_path.write_text(
        service.config_path.read_text(encoding="utf-8") + "\n# хвостовой комментарий\n",
        encoding="utf-8",
    )

    await service.write_config({"runtime.autonomy": "supervised"})

    text = service.config_path.read_text(encoding="utf-8")
    assert "# хвостовой комментарий" in text


@pytest.mark.asyncio
async def test_config_write_persists_and_reloads(service: GatewayService) -> None:
    await service.write_config({"runtime.autonomy": "supervised", "runtime.max_iterations": 7})

    reloaded = load_config(project_dir=service.workspace)
    assert reloaded.runtime.autonomy.value == "supervised"
    assert reloaded.runtime.max_iterations == 7


@pytest.mark.asyncio
async def test_write_config_reloads_snapshot_used_by_runs(
    service: GatewayService, tmp_path: Path
) -> None:
    """Правка исполнителя должна действовать без перезапуска svarog serve."""
    assert service.cfg.runtime.max_iterations != 7
    view = await service.write_config({"runtime.max_iterations": 7})
    assert view.restart_required is False
    assert service.cfg.runtime.max_iterations == 7
    assert service._runner.cfg.runtime.max_iterations == 7


@pytest.mark.asyncio
async def test_write_config_defers_reload_while_run_is_live(service: GatewayService) -> None:
    """Живой run держит свой снимок конфига до конца (ADR-0015 §0.4).

    Правка при этом всё равно пишется в файл — иначе следующий рестарт
    покажет то, что человек уже якобы сохранил, но что незаметно потерялось.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from svarog_harness.storage.models import Run, RunState

    session = await service.create_session(title="занят")
    run_id = await service.send_message(session.session_id, "задача", None)
    # Run падает на недоступном провайдере; дожидаемся, чтобы фоновая задача
    # не осталась висеть после теста. Живое состояние для проверки ветки
    # проставляем сами — важна ветка, а не гонка с фоновой задачей.
    for _ in range(400):
        if (await service.get_run(run_id)).state in {"completed", "failed"}:
            break
        await asyncio.sleep(0.01)

    async def seed(db: AsyncSession) -> None:
        db.add(
            Run(
                id="run-live-config",
                session_id=session.session_id,
                task="идёт",
                state=RunState.RUNNING,
                autonomy="supervised",
            )
        )
        await db.commit()

    await service._read(seed)

    before = service.cfg.runtime.max_iterations
    view = await service.write_config({"runtime.max_iterations": before + 1})

    assert view.restart_required is True
    assert service.cfg.runtime.max_iterations == before


@pytest.mark.asyncio
async def test_config_rejects_value_schema_would_reject(service: GatewayService) -> None:
    from svarog_harness.config.loader import ConfigError

    # max_iterations объявлен как gt=0 — ноль схема не примет.
    with pytest.raises(ConfigError):
        service.preview_config({"runtime.max_iterations": 0})


@pytest.mark.asyncio
async def test_config_rejects_field_outside_form(service: GatewayService) -> None:
    with pytest.raises(ValueError, match="недоступно"):
        service.preview_config({"storage.db_path": "/tmp/x.db"})


def test_config_endpoints(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))

    assert client.get("/config").status_code == 200

    preview = client.post("/config/preview", json={"values": {"runtime.autonomy": "auto"}})
    assert preview.status_code == 200
    assert preview.json()["changes"] > 0

    bad = client.post("/config/preview", json={"values": {"runtime.max_iterations": 0}})
    assert bad.status_code == 422


def test_secrets_never_return_values(
    service: GatewayService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверка бессмысленна на пустом store — секрет должен реально существовать."""
    import json

    secrets_file = tmp_path / ".svarog" / "secrets.json"
    secrets_file.parent.mkdir(parents=True, exist_ok=True)
    secrets_file.write_text(json.dumps({"PROVIDER_API_KEY": "sk-очень-секретно"}), encoding="utf-8")
    # FileSecretStore читает файл в конструкторе — сервис нужен созданный после.
    fresh = GatewayService(service.cfg, service.workspace)
    client = TestClient(create_app(service=fresh))

    response = client.get("/secrets")

    assert response.status_code == 200
    body = response.json()
    assert [s["name"] for s in body] == ["PROVIDER_API_KEY"], body
    assert body[0]["present"] is True
    assert "sk-очень-секретно" not in response.text


def test_root_explains_when_bundle_missing(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Пустой каталог: ни упакованного бандла, ни сборки в чекауте.
    monkeypatch.setenv("SVAROG_WEB_DIST", str(tmp_path / "нет-сборки"))
    client = TestClient(create_app(service=service))

    response = client.get("/")

    assert response.status_code == 404
    assert "npm --prefix web run build" in response.json()["detail"]


def test_root_serves_bundle_when_present(
    service: GatewayService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Сварог</title>", encoding="utf-8")
    monkeypatch.setenv("SVAROG_WEB_DIST", str(dist))

    client = TestClient(create_app(service=service))
    response = client.get("/")

    assert response.status_code == 200
    assert "Сварог" in response.text


def test_cors_disabled_by_default(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))
    response = client.get("/healthz", headers={"Origin": "http://localhost:5173"})
    assert "access-control-allow-origin" not in response.headers


def test_cors_env_name_does_not_break_config_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Имя переменной CORS не должно попадать в namespace SVAROG_*.

    pydantic-settings разбирает SVAROG_GATEWAY__* как поле секции gateway,
    а GatewayConfig — StrictModel: такая переменная роняла `svarog serve`
    ещё до старта, по документированному же рецепту включения CORS.
    """
    ws = tmp_path / "ws-cors"
    ws.mkdir()
    _write_config(ws, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GORN_CORS_ORIGINS", "http://localhost:5173")

    # Не бросает — это и есть проверка.
    load_config(project_dir=ws)


def test_cors_rejects_wildcard(service: GatewayService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GORN_CORS_ORIGINS", "*")
    with pytest.raises(ValueError, match="не поддерживается"):
        create_app(service=service)


def test_cors_enabled_by_env(service: GatewayService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GORN_CORS_ORIGINS", "http://localhost:5173")
    client = TestClient(create_app(service=service))

    response = client.get("/healthz", headers={"Origin": "http://localhost:5173"})

    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


@pytest.mark.asyncio
async def test_approval_event_published(service: GatewayService) -> None:
    from svarog_harness.storage.models import Approval

    published: list[dict[str, object]] = []
    service.events.publish = lambda run_id, event: published.append(event)  # type: ignore[method-assign]

    started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    holder = _RunHolder()
    holder.run_id = "run-1"
    hooks = service._event_hooks(holder, started)

    approval = Approval(
        id="ap-1",
        run_id="run-1",
        action_type="run_shell",
        payload={"command": "uv run pytest -q"},
    )
    assert hooks.on_approval_requested is not None
    hooks.on_approval_requested(approval)

    assert published == [
        {
            "type": "approval_required",
            "approval_id": "ap-1",
            "action_type": "run_shell",
            "payload": {"command": "uv run pytest -q"},
        }
    ]


@pytest.mark.asyncio
async def test_config_form_shows_file_not_startup_snapshot(service: GatewayService) -> None:
    """После сохранения форма обязана показать новое значение, а не старое.

    `GatewayService.cfg` — снимок при старте; если рисовать форму из него,
    значение откатывается на глазах у человека.
    """
    await service.write_config({"runtime.autonomy": "supervised"})

    view = service.describe_config()

    field = next(
        f for section in view.sections for f in section.fields if f.path == "runtime.autonomy"
    )
    assert field.value == "supervised"


@pytest.mark.asyncio
async def test_config_refuses_when_patch_would_break_file(service: GatewayService) -> None:
    """Flow-style секцию построчный патчер корректно не осилит — отказ, не порча."""
    from svarog_harness.config.loader import ConfigError

    service.config_path.write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox: {type: local-trusted}\n",
        encoding="utf-8",
    )
    before = service.config_path.read_text(encoding="utf-8")

    with pytest.raises(ConfigError, match=r"сломала бы|вручную"):
        await service.write_config({"sandbox.timeout_sec": 300})

    assert service.config_path.read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_native_loop_notifies_about_approval(service: GatewayService) -> None:
    """Нативный цикл — исполнитель по умолчанию; без этого гейт в вебе мёртв.

    Хук on_approval_requested потребляет только внешний агент, поэтому у
    нативного цикла есть отдельное уведомление on_approval_created.
    """
    from svarog_harness.storage.models import Approval

    published: list[dict[str, object]] = []
    service.events.publish = lambda run_id, event: published.append(event)  # type: ignore[method-assign]

    started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    holder = _RunHolder()
    holder.run_id = "run-1"
    hooks = service._event_hooks(holder, started)

    assert hooks.on_approval_created is not None, "нативный цикл остался без уведомления"
    hooks.on_approval_created(
        Approval(
            id="ap-2",
            run_id="run-1",
            action_type="run_shell",
            payload={"command": "uv run pytest -q"},
        )
    )

    assert published == [
        {
            "type": "approval_required",
            "approval_id": "ap-2",
            "action_type": "run_shell",
            "payload": {"command": "uv run pytest -q"},
        }
    ]


def test_agent_loop_accepts_approval_notification_hook() -> None:
    """Хук должен доезжать до самого цикла, а не только до RunHooks."""
    import inspect

    from svarog_harness.runtime.loop import AgentLoop
    from svarog_harness.runtime.run_assembly import RunHooks

    assert "on_approval_created" in inspect.signature(AgentLoop.__init__).parameters
    assert "on_approval_created" in RunHooks.__dataclass_fields__


@pytest.mark.asyncio
async def test_session_activity_bumps_updated_at(service: GatewayService) -> None:
    """Навигатор сортирует по свежести — значит активность должна её двигать.

    `updated_at` меняется только при UPDATE строки Session, а run'ы её не
    трогают: без явного касания вчерашняя сессия с сообщением минуту назад
    оказывалась бы в «Ранее» с холодной шкалой накала.
    """
    old = await service.create_session(title="старая")
    fresh = await service.create_session(title="свежая")

    # Сообщение в старую сессию должно поднять её наверх списка.
    with contextlib.suppress(Exception):
        # Реального LLM нет: важно, что касание произошло до запуска run'а.
        await service.send_message(old.session_id, "привет", None)

    listed = await service.list_sessions()
    titles = [s.title for s in listed]
    assert titles[:1] == ["старая"], f"ожидалась старая сверху: {titles}; свежая — {fresh.title}"


def test_send_message_maps_setup_errors_to_422(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ошибки настройки запуска — 422 с текстом, а не 500 с трейсбеком.

    Найдено реальным проходом: workspace, совпавший с agent-home, и режим
    автономии, которого не умеет исполнитель, оба валились пятисоткой, а
    интерфейсу нечего было показать.
    """
    from svarog_harness.config.paths import WorkspaceLayoutError
    from svarog_harness.sandbox.base import SandboxError

    client = TestClient(create_app(service=service))
    session = client.post("/sessions", json={"title": "проба"}).json()

    for error in (
        SandboxError("режим 'supervised' не поддерживается исполнителем"),
        WorkspaceLayoutError("control-plane пересекается с workspace"),
    ):

        async def boom(*args: object, error: Exception = error, **kwargs: object) -> str:
            raise error

        monkeypatch.setattr(service, "send_message", boom)
        response = client.post(
            f"/sessions/{session['session_id']}/messages", json={"text": "привет"}
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == str(error)


@pytest.mark.asyncio
async def test_delete_session_removes_it_with_runs(service: GatewayService) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from svarog_harness.storage.models import Run, RunState

    session = await service.create_session(title="на удаление")

    async def seed(db: AsyncSession) -> None:
        db.add(
            Run(
                id="run-del",
                session_id=session.session_id,
                task="что-то",
                state=RunState.COMPLETED,
                autonomy="supervised",
            )
        )
        await db.commit()

    await service._read(seed)

    await service.delete_session(session.session_id)

    assert [s.session_id for s in await service.list_sessions()] == []
    # Каскад: run исчез вместе с сессией, осиротевших записей не осталось.
    assert [r.run_id for r in await service.list_runs()] == []


@pytest.mark.asyncio
async def test_delete_session_refuses_while_run_is_live(service: GatewayService) -> None:
    """Удалять историю запуска, который прямо сейчас правит дерево, нельзя."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from svarog_harness.gateway.service import SessionBusyError
    from svarog_harness.storage.models import Run, RunState

    session = await service.create_session(title="занятая")

    async def seed(db: AsyncSession) -> None:
        db.add(
            Run(
                id="run-live",
                session_id=session.session_id,
                task="идёт",
                state=RunState.RUNNING,
                autonomy="supervised",
            )
        )
        await db.commit()

    await service._read(seed)

    with pytest.raises(SessionBusyError, match="идёт запуск"):
        await service.delete_session(session.session_id)

    assert len(await service.list_sessions()) == 1


def test_delete_session_endpoint(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))
    created = client.post("/sessions", json={"title": "через API"}).json()

    assert client.delete(f"/sessions/{created['session_id']}").status_code == 204
    assert client.get("/sessions").json() == []
    assert client.delete("/sessions/нет-такой").status_code == 404


@pytest.mark.asyncio
async def test_list_sessions_reports_live_state_for_indicator(
    service: GatewayService,
) -> None:
    """Индикатор занятости строится на last_state — он обязан быть точным."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from svarog_harness.storage.models import Run, RunState

    session = await service.create_session(title="занятая")

    async def seed(db: AsyncSession) -> None:
        db.add(
            Run(
                id="run-x",
                session_id=session.session_id,
                task="идёт",
                state=RunState.RUNNING,
                autonomy="supervised",
            )
        )
        await db.commit()

    await service._read(seed)

    listed = await service.list_sessions()
    assert listed[0].last_state == "running"
    assert listed[0].runs_count == 1
