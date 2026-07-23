"""Тесты cloud-режима (ADR-0017 Фаза 1): named workspaces, git-провижн, diff, GC."""

import asyncio
import os
import tarfile
import time
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import ModelsConfig
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app
from svarog_harness.gitflow import provision
from svarog_harness.gitflow.provision import (
    CloneError,
    RepoUrlError,
    UnknownWorkspaceError,
    WorkspaceNameError,
    provision_clone,
    resolve_workspace_file,
    sweep_task_workspaces,
    validate_repo_url,
    validate_workspace_name,
)
from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.runtime import orchestrator
from svarog_harness.storage.models import Run, RunState, Session, utcnow


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


def _write_config(ws: Path, tmp_path: Path, *, extra: str = "") -> None:
    db_path = tmp_path / "state" / "svarog.db"
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {db_path}\n" + extra,
        encoding="utf-8",
    )


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayService:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config(project_dir=ws)
    return GatewayService(cfg, ws)


@pytest.fixture
def client(service: GatewayService) -> TestClient:
    return TestClient(create_app(service))


def _patch_provider(monkeypatch: pytest.MonkeyPatch, turns: list[CompletionResult]) -> None:
    provider = ScriptedProvider(turns)

    def fake_default_provider(models_cfg: ModelsConfig, store: object = None) -> ModelProvider:
        return provider

    monkeypatch.setattr(orchestrator, "default_provider", fake_default_provider)


def _write_turn(path: str = "out.txt", content: str = "готово") -> CompletionResult:
    return CompletionResult(
        content="",
        tool_calls=(
            ToolCallRequest(
                id="c1",
                name="write_file",
                arguments_json=f'{{"path": "{path}", "content": "{content}"}}',
            ),
        ),
        usage=Usage(10, 5),
    )


def _final_turn(text: str = "сделано") -> CompletionResult:
    return CompletionResult(content=text, usage=Usage(10, 5), finish_reason="stop")


async def _wait_completed(service: GatewayService, run_id: str) -> str:
    """Дождаться терминального состояния run'а И его фоновой пост-обработки.

    Состояние в БД переключается раньше, чем фоновая задача сервиса отпускает
    workspace: дренаж памяти и автокоммит держат `.git/index.lock` ещё
    какое-то время. Тест, который сразу после этого сам лезет в git того же
    каталога, ловит `Unable to create index.lock` — поэтому ждём и задачи
    (`wait_for_background` заведён ровно под этот случай).
    """
    for _ in range(600):
        detail = await service.get_run(run_id)
        if detail.state in {"completed", "failed"}:
            await service.wait_for_background()
            return detail.state
        await asyncio.sleep(0.01)
    await service.wait_for_background()
    return (await service.get_run(run_id)).state


async def _run_workspace(service: GatewayService, run_id: str) -> str | None:
    async def action(db: AsyncSession) -> str | None:
        from svarog_harness.trace.lookup import find_run_by_prefix

        return (await find_run_by_prefix(db, run_id)).workspace

    return await service._read(action)


async def _insert_running_run(service: GatewayService, workspace: Path) -> None:
    """Живой RUNNING-run с fresh heartbeat — занимает lease workspace'а."""

    async def action(db: AsyncSession) -> None:
        sess = Session(title="t")
        db.add(sess)
        await db.flush()
        db.add(
            Run(
                session_id=sess.id,
                state=RunState.RUNNING,
                task="занято",
                autonomy="yolo",
                workspace=str(workspace),
                heartbeat_at=utcnow(),
            )
        )
        await db.commit()

    await service._runner.with_db(action)


# --- валидация ------------------------------------------------------------


def test_repo_url_validation() -> None:
    assert validate_repo_url("https://github.com/org/repo.git")
    assert validate_repo_url("ssh://git@host.example/org/repo.git")
    assert validate_repo_url("git@github.com:org/repo.git")
    for bad in (
        "file:///etc/passwd",
        "/local/path/repo",
        "ext::sh -c date",
        "http://insecure.example/repo.git",
        "../relative",
    ):
        with pytest.raises(RepoUrlError):
            validate_repo_url(bad)


def test_workspace_name_validation() -> None:
    assert validate_workspace_name("proj-1") == "proj-1"
    for bad in ("..", "a/b", "A", "", "-lead", "имя"):
        with pytest.raises(WorkspaceNameError):
            validate_workspace_name(bad)


# --- named workspaces: CRUD и confinement ---------------------------------


def test_workspace_crud_api(client: TestClient, service: GatewayService) -> None:
    assert client.post("/workspaces", json={"name": "proj"}).status_code == 201
    assert client.post("/workspaces", json={"name": "proj"}).status_code == 409
    assert client.post("/workspaces", json={"name": "Bad/Name"}).status_code == 422

    listed = client.get("/workspaces").json()
    assert [w["name"] for w in listed] == ["proj"]
    assert listed[0]["busy"] is False

    assert client.delete("/workspaces/proj").status_code == 204
    assert client.get("/workspaces").json() == []
    assert client.delete("/workspaces/proj").status_code == 404


def test_workspace_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, tmp_path, extra="cloud:\n  max_named_workspaces: 2\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    service = GatewayService(load_config(project_dir=ws), ws)
    client = TestClient(create_app(service))
    assert client.post("/workspaces", json={"name": "a"}).status_code == 201
    assert client.post("/workspaces", json={"name": "b"}).status_code == 201
    assert client.post("/workspaces", json={"name": "c"}).status_code == 429


def test_workspace_files_confinement(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "named" / "proj" / "sub").mkdir(parents=True)
    (root / "named" / "proj" / "sub" / "f.txt").write_text("data", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("секрет хоста", encoding="utf-8")

    assert resolve_workspace_file(root, "proj", "sub/f.txt").read_text(encoding="utf-8") == "data"
    with pytest.raises(WorkspaceNameError):
        resolve_workspace_file(root, "proj", "..")
    with pytest.raises(WorkspaceNameError):
        resolve_workspace_file(root, "proj", "../../outside.txt")
    # Symlink-escape: ссылка внутри workspace на файл снаружи.
    (root / "named" / "proj" / "leak").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(WorkspaceNameError):
        resolve_workspace_file(root, "proj", "leak")
    with pytest.raises(UnknownWorkspaceError):
        resolve_workspace_file(root, "ghost", "f.txt")


def test_workspace_files_and_archive_api(client: TestClient, service: GatewayService) -> None:
    client.post("/workspaces", json={"name": "proj"})
    ws = service.workspace / "named" / "proj"
    (ws / "sub").mkdir()
    (ws / "sub" / "result.txt").write_text("итог", encoding="utf-8")

    listing = client.get("/workspaces/proj/files").json()
    assert [e["name"] for e in listing["entries"]] == ["sub"]
    resp = client.get("/workspaces/proj/files", params={"path": "sub/result.txt"})
    assert resp.status_code == 200
    assert resp.text == "итог"
    assert (
        client.get("/workspaces/proj/files", params={"path": "../../svarog.yaml"}).status_code
        == 422
    )
    assert client.get("/workspaces/proj/files", params={"path": "нет.txt"}).status_code == 404

    archive = client.get("/workspaces/proj/archive")
    assert archive.status_code == 200
    with tarfile.open(fileobj=BytesIO(archive.content), mode="r:gz") as tar:
        assert "proj/sub/result.txt" in tar.getnames()


# --- runs в per-run workspaces --------------------------------------------


async def test_run_in_named_workspace(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    await service.create_workspace("proj")
    _patch_provider(monkeypatch, [_write_turn(), _final_turn()])
    run_id = await service.create_run("создай файл", None, workspace_name="proj")
    assert await _wait_completed(service, run_id) == "completed"

    named = service.workspace / "named" / "proj"
    assert (named / "out.txt").read_text(encoding="utf-8") == "готово"
    assert not (service.workspace / "out.txt").exists()  # не в workspace сервиса
    assert Path(await _run_workspace(service, run_id) or "") == named.resolve()


async def test_run_unknown_workspace_and_conflict(service: GatewayService) -> None:
    with pytest.raises(UnknownWorkspaceError):
        await service.create_run("задача", None, workspace_name="ghost")


def test_api_run_unknown_workspace_404_and_mutex_422(client: TestClient) -> None:
    resp = client.post("/runs", json={"task": "т", "workspace": "ghost"})
    assert resp.status_code == 404
    resp = client.post(
        "/runs",
        json={"task": "т", "workspace": "a", "repo": {"url": "https://h/r.git"}},
    )
    assert resp.status_code == 422  # repo и workspace взаимоисключающие


async def test_named_workspace_busy_409(client: TestClient, service: GatewayService) -> None:
    await service.create_workspace("proj")
    named = (service.workspace / "named" / "proj").resolve()
    await _insert_running_run(service, named)

    resp = client.post("/runs", json={"task": "второй", "workspace": "proj"})
    assert resp.status_code == 409
    assert client.delete("/workspaces/proj").status_code == 409
    busy = {w["name"]: w["busy"] for w in client.get("/workspaces").json()}
    assert busy["proj"] is True


async def test_approval_resume_in_named_workspace(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume после approval остаётся в named workspace (шов _runner_for_resume)."""
    await service.create_workspace("proj")
    _patch_provider(
        monkeypatch,
        [
            CompletionResult(
                content="",
                tool_calls=(
                    ToolCallRequest(
                        id="c1",
                        name="request_approval",
                        arguments_json='{"action": "рискованный шаг", "details": "деплой"}',
                    ),
                ),
                usage=Usage(10, 5),
            ),
            _write_turn("after.txt", "после resume"),
            _final_turn(),
        ],
    )
    run_id = await service.create_run("рискованная", None, workspace_name="proj")
    for _ in range(600):
        if (await service.get_run(run_id)).state == "waiting_approval":
            break
        await asyncio.sleep(0.01)
    pending = await service.list_pending_approvals()
    assert pending and pending[0].run_id == run_id
    await service.decide_approval(pending[0].approval_id, approved=True, reason=None)
    await service.resume_run(run_id)
    assert await _wait_completed(service, run_id) == "completed"
    named = service.workspace / "named" / "proj"
    assert (named / "after.txt").read_text(encoding="utf-8") == "после resume"


# --- git-провижн ----------------------------------------------------------


async def _make_source_repo(path: Path) -> None:
    repo = GitRepo(path)
    path.mkdir(parents=True)
    await repo.init()
    await repo.ensure_identity()
    (path / "README.md").write_text("исходный репо", encoding="utf-8")
    await repo.add_all()
    await repo.commit("init")


def _allow_local_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тестовый шов: локальный путь как «remote» (в проде отклонён валидатором)."""
    monkeypatch.setattr(provision, "_PROTOCOL_FLAGS", ())
    monkeypatch.setattr(provision, "validate_repo_url", lambda url: url)


async def test_provision_clone_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    await _make_source_repo(src)
    _allow_local_clone(monkeypatch)
    dest = tmp_path / "tasks" / "t1"
    await provision_clone(str(src), dest)
    assert (dest / "README.md").read_text(encoding="utf-8") == "исходный репо"
    with pytest.raises(CloneError):  # dest уже существует
        await provision_clone(str(src), dest)


async def test_provision_clone_blocks_file_transport(tmp_path: Path) -> None:
    src = tmp_path / "src"
    await _make_source_repo(src)
    with pytest.raises(RepoUrlError):
        await provision_clone(str(src), tmp_path / "t")  # локальный путь — отказ
    with pytest.raises(RepoUrlError):
        await provision_clone(f"file://{src}", tmp_path / "t2")


async def test_run_from_cloned_repo(
    service: GatewayService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src"
    await _make_source_repo(src)
    _allow_local_clone(monkeypatch)
    _patch_provider(monkeypatch, [_write_turn("new.txt", "изменение"), _final_turn()])

    from svarog_harness.gateway.models import RepoSpec

    run_id = await service.create_run("правка репо", None, repo=RepoSpec(url=str(src)))
    assert await _wait_completed(service, run_id) == "completed"

    ws = Path(await _run_workspace(service, run_id) or "")
    assert ws.parent == (service.workspace / "tasks").resolve()
    assert (ws / "README.md").exists()  # клон источника
    assert (ws / "new.txt").read_text(encoding="utf-8") == "изменение"


# --- diff и GC ------------------------------------------------------------


async def test_run_diff(service: GatewayService, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = GitRepo(service.workspace)
    await repo.init()
    await repo.ensure_identity()
    (service.workspace / "base.txt").write_text("база\n", encoding="utf-8")
    await repo.add_all()
    await repo.commit("base")

    _patch_provider(monkeypatch, [_final_turn("ничего не менял")])
    run_id = await service.create_run("посмотри", None)
    assert await _wait_completed(service, run_id) == "completed"

    # Коммит шага run'а (Run-Id trailer, как ставит commit_step Flow C).
    (service.workspace / "base.txt").write_text("изменено run'ом\n", encoding="utf-8")
    await repo.add_all()
    await repo.commit("шаг", trailers={"Run-Id": run_id})
    # Плюс незакоммиченное изменение рабочего дерева.
    (service.workspace / "wip.txt").write_text("не закоммичено\n", encoding="utf-8")
    await repo.add_all()

    diff = await service.run_diff(run_id)
    assert "изменено run'ом" in diff.committed
    assert "не закоммичено" in diff.uncommitted

    empty = await service.run_diff(run_id[:8])  # префикс тоже резолвится
    assert empty.run_id == run_id


async def test_run_diff_without_git(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(monkeypatch, [_final_turn()])
    run_id = await service.create_run("задача", None)
    assert await _wait_completed(service, run_id) == "completed"
    diff = await service.run_diff(run_id)
    assert diff.committed == "" and diff.uncommitted == ""


def _age_dir(path: Path, days: int) -> None:
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


async def test_sweep_task_workspaces(service: GatewayService) -> None:
    tasks = service.workspace / "tasks"
    old = tasks / "old-run-a1b2"
    fresh = tasks / "fresh-run-c3d4"
    active = tasks / "active-run-e5f6"
    named_old = service.workspace / "named" / "keeper"
    for d in (old, fresh, active, named_old):
        d.mkdir(parents=True)
    _age_dir(old, days=30)
    _age_dir(active, days=30)
    _age_dir(named_old, days=365)
    await _insert_running_run(service, active.resolve())

    removed = await service.sweep_workspaces()

    assert [p.name for p in removed] == ["old-run-a1b2"]
    assert not old.exists()
    assert fresh.exists()
    assert active.exists()  # живой run — не трогаем
    assert named_old.exists()  # named GC не подлежит никогда


def test_sweep_respects_retention_zero(tmp_path: Path) -> None:
    root = tmp_path / "root"
    stale = root / "tasks" / "ancient"
    stale.mkdir(parents=True)
    _age_dir(stale, days=999)
    assert sweep_task_workspaces(root, retention_days=0, active=set()) == []
    assert stale.exists()


def test_stale_cutoff_math(tmp_path: Path) -> None:
    root = tmp_path / "root"
    d = root / "tasks" / "borderline"
    d.mkdir(parents=True)
    _age_dir(d, days=10)
    assert provision.stale_task_workspaces(root, retention_days=14, active=set()) == []
    assert provision.stale_task_workspaces(root, retention_days=7, active=set()) == [d]


def test_strip_secrets() -> None:
    msg = provision._strip_secrets("fatal: https://user:tok123@host/r.git", "user:tok123")
    assert "tok123" not in msg
    assert "***" in msg


# --- граница workspace против родительского git (регрессия симуляции remote) --


async def test_flow_c_disabled_inside_foreign_repo(tmp_path: Path) -> None:
    """Workspace в поддиректории чужого репо: Flow C выключен, commit_step — None."""
    from svarog_harness.config.schema import GitConfig
    from svarog_harness.gitflow.workspace import WorkspaceFlow

    root = tmp_path / "parent"
    sub = root / "named" / "proj"
    sub.mkdir(parents=True)
    repo = GitRepo(root)
    await repo.init()
    await repo.ensure_identity()
    (root / "config.yaml").write_text("secret_ref: X\n", encoding="utf-8")
    await repo.add_all()
    await repo.commit("init")

    flow = WorkspaceFlow(GitRepo(sub), GitConfig())
    prep = await flow.start("задача")
    assert prep.is_git is False
    assert "не пересекают границу workspace" in prep.note

    (sub / "result.txt").write_text("итог", encoding="utf-8")
    assert await flow.commit_step("svarog: шаг") is None
    # Родительский репо не тронут: ни веток svarog/*, ни новых коммитов.
    _, out, _ = await repo._git("branch", "--list", "svarog/*")
    assert out.strip() == ""
    _, log, _ = await repo._git("log", "--oneline")
    assert len(log.strip().splitlines()) == 1


async def test_named_workspace_run_does_not_touch_parent_repo(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run в named workspace внутри git-корня сервиса: без веток/коммитов в
    родительском репо и без его содержимого в diff (находка симуляции remote)."""
    repo = GitRepo(service.workspace)
    await repo.init()
    await repo.ensure_identity()
    (service.workspace / "server.txt").write_text("серверный файл\n", encoding="utf-8")
    await repo.add_all()
    await repo.commit("init")

    await service.create_workspace("proj")
    _patch_provider(monkeypatch, [_write_turn("out.txt", "готово"), _final_turn()])
    run_id = await service.create_run("создай файл", None, workspace_name="proj")
    assert await _wait_completed(service, run_id) == "completed"
    assert (service.workspace / "named" / "proj" / "out.txt").exists()

    # Родительский репо сервиса не тронут.
    _, branches, _ = await repo._git("branch", "--list", "svarog/*")
    assert branches.strip() == ""
    _, log, _ = await repo._git("log", "--oneline", "--all")
    assert len(log.strip().splitlines()) == 1

    # Diff run'а не показывает родительский репозиторий.
    diff = await service.run_diff(run_id)
    assert diff.committed == "" and diff.uncommitted == ""
