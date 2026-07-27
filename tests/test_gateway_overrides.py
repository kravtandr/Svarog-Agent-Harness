"""Override исполнителя/провайдера/модели в сообщении чата (план 2026-07-28)."""

import asyncio
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import AutonomyMode
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app
from svarog_harness.gateway.overrides import (
    OVERRIDE_META_KEY,
    OverrideError,
    RunOverride,
    apply_override,
)
from svarog_harness.runtime import orchestrator
from svarog_harness.runtime.config_snapshot import CONFIG_HASH_META_KEY, config_digest
from svarog_harness.runtime.orchestrator import RunHooks, TaskRunner
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.tools.child_tools import SpawnChildRunArgs
from svarog_harness.trace.lookup import find_run_by_prefix
from svarog_harness.trace.recorder import TraceRecorder


def _config(tmp_path: Path, extra: str = "") -> object:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "    router:\n"
        "      base_url: https://openrouter.ai/api/v1\n"
        "      model: deepseek/deepseek-v4-flash\n"
        "      input_usd_per_mtok: 1.0\n"
        "      output_usd_per_mtok: 2.0\n"
        "sandbox:\n  type: local-trusted\n" + extra,
        encoding="utf-8",
    )
    # Исключить user-level конфиг, чтобы тесты не зависели от ~/.svarog/svarog.yaml.
    return load_config(project_dir=ws, user_config_path=tmp_path / "nonexistent")


def test_empty_override_returns_config_unchanged(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert RunOverride().is_empty()
    assert apply_override(cfg, RunOverride()) is cfg


def test_provider_switches_default(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    derived = apply_override(cfg, RunOverride(provider="router"))
    assert derived.models.default == "router"
    assert cfg.models.default == "local", "исходный конфиг не мутируется"


def test_model_applies_to_named_provider(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    derived = apply_override(cfg, RunOverride(provider="router", model="anthropic/claude"))
    assert derived.models.providers["router"].model == "anthropic/claude"
    assert derived.models.providers["local"].model == "fake-model"


def test_model_without_provider_applies_to_default(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    derived = apply_override(cfg, RunOverride(model="qwen3"))
    assert derived.models.providers["local"].model == "qwen3"


def test_prices_replace_provider_prices(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    derived = apply_override(cfg, RunOverride(provider="router", model="x/y"), prices=(0.5, 1.5))
    assert derived.models.providers["router"].input_usd_per_mtok == 0.5
    assert derived.models.providers["router"].output_usd_per_mtok == 1.5


def test_unknown_provider_rejected_with_known_names(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(OverrideError) as exc:
        apply_override(cfg, RunOverride(provider="нет-такого"))
    assert "local" in str(exc.value) and "router" in str(exc.value)


def test_external_without_section_rejected(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(OverrideError, match=re.escape("executor.external")):
        apply_override(cfg, RunOverride(executor="external"))


def test_external_requires_docker_sandbox(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        "executor:\n  type: native\n  external:\n    image: svarog/agent:1\n",
    )
    with pytest.raises(OverrideError, match="sandbox"):
        apply_override(cfg, RunOverride(executor="external"))


def test_external_allowed_with_docker(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: docker\n"
        "executor:\n  type: native\n  external:\n    image: svarog/agent:1\n",
        encoding="utf-8",
    )
    cfg = load_config(project_dir=ws, user_config_path=tmp_path / "nonexistent")
    derived = apply_override(cfg, RunOverride(executor="external"))
    assert derived.executor.type == "external"


def test_meta_round_trip_keeps_only_set_fields() -> None:
    ov = RunOverride(provider="router", model="x/y")
    meta = {OVERRIDE_META_KEY: ov.to_meta()}
    assert ov.to_meta() == {"provider": "router", "model": "x/y"}
    assert RunOverride.from_meta(meta) == ov
    assert RunOverride.from_meta(None).is_empty()
    assert RunOverride.from_meta({}).is_empty()
    assert RunOverride.from_meta({OVERRIDE_META_KEY: {"мусор": 1}}).is_empty()


@pytest.mark.asyncio
async def test_start_run_stores_extra_meta(tmp_path: Path) -> None:
    db_path = tmp_path / "svarog.db"
    init_db(db_path)
    engine = create_engine(db_path)
    factory = create_session_factory(engine)
    async with factory() as db:
        run = await TraceRecorder(db).start_run(
            task="задача",
            autonomy="yolo",
            model="fake-model",
            extra_meta={OVERRIDE_META_KEY: {"provider": "router"}},
        )
    assert run.meta[OVERRIDE_META_KEY] == {"provider": "router"}
    assert run.meta["model"] == "fake-model", "штатные ключи не затёрты"
    await engine.dispose()


def _git(ws: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(ws), *args], check=True, capture_output=True, text=True)


async def test_spawn_child_run_passes_run_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_meta родителя должен долететь до TaskRunner дочернего run'а.

    В отличие от parent_run_id (передаётся per-call и уже проверен на
    дочерних runs), run_meta запечён в конструкторе TaskRunner/RunAssembly —
    его легко забыть протащить в spawn_child_run, а забытым он делает
    child.meta несогласованным с child.config_hash, унаследовавшим эффект
    override через model_copy от self._cfg. Полный e2e (ScriptedProvider,
    выполнение ребёнка до конца) непропорционален этой проверке: достаточно
    убедиться, что TaskRunner дочернего run'а сконструирован с тем же
    run_meta, что и у родителя — тест обрывает spawn_child_run сразу после
    этой точки шпионским __init__.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
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
    (ws / "README.md").write_text("проект\n", encoding="utf-8")
    _git(ws, "init", "-b", "main")
    _git(ws, "config", "user.email", "t@t")
    _git(ws, "config", "user.name", "t")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "init")

    cfg = load_config(project_dir=ws, user_config_path=tmp_path / "nonexistent")
    run_meta = {OVERRIDE_META_KEY: {"provider": "router"}}
    runner = TaskRunner(cfg, ws, run_meta=run_meta)

    captured: list[dict[str, object] | None] = []

    class _StopAfterCaptureError(Exception):
        """Сигнал «конструктор ребёнка вызван» — дальше эту ветку не гоняем."""

    def spy_init(self: TaskRunner, cfg: object, workspace: object, **kwargs: object) -> None:
        captured.append(kwargs.get("run_meta"))
        raise _StopAfterCaptureError()

    monkeypatch.setattr(orchestrator.TaskRunner, "__init__", spy_init)

    async def action(db: AsyncSession) -> None:
        recorder = TraceRecorder(db)
        parent = await recorder.start_run(
            task="родитель", autonomy="yolo", model="m", workspace=str(ws)
        )
        with pytest.raises(_StopAfterCaptureError):
            await runner.spawn_child_run(
                recorder,
                parent,
                AutonomyMode.YOLO,
                SpawnChildRunArgs(task="подзадача"),
                RunHooks(),
            )

    await runner.with_db(action)
    assert captured == [run_meta], "run_meta родителя не долетел до TaskRunner ребёнка"


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayService:
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
        "    router:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: router-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {db_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    return GatewayService(load_config(project_dir=ws), ws)


@pytest.mark.asyncio
async def test_override_survives_resume_without_config_drift(
    service: GatewayService,
) -> None:
    """Дайджест конфига при resume совпадает со снимком старта.

    Это и есть гарантия, ради которой override кладётся в Run.meta:
    `_assert_config_unchanged` сверяет хеши и fail-closed при расхождении
    (ADR-0015 §0.4). Запуск падает на недоступном провайдере — неважно:
    строка run'а со снимком создаётся до первого обращения к модели.
    """
    session = await service.create_session(title="с override")
    run_id = await service.send_message(
        session.session_id, "задача", None, RunOverride(provider="router")
    )

    async def read(db):
        run = await find_run_by_prefix(db, run_id)
        return dict(run.meta or {})

    meta = await service._read(read)
    assert meta[OVERRIDE_META_KEY] == {"provider": "router"}

    runner = await service._runner_for_run(run_id)
    assert runner.cfg.models.default == "router"
    assert config_digest(runner.cfg, service.workspace) == meta[CONFIG_HASH_META_KEY]


@pytest.mark.asyncio
async def test_run_without_override_keeps_config_default(
    service: GatewayService,
) -> None:
    session = await service.create_session(title="без override")
    run_id = await service.send_message(session.session_id, "задача", None)

    runner = await service._runner_for_run(run_id)
    assert runner.cfg.models.default == "local"


@pytest.mark.asyncio
async def test_unknown_provider_returns_422(service: GatewayService) -> None:
    session = await service.create_session(title="ошибка")
    client = TestClient(create_app(service=service))
    response = client.post(
        f"/sessions/{session.session_id}/messages",
        json={"text": "задача", "provider": "нет-такого"},
    )
    assert response.status_code == 422
    assert "нет-такого" in response.json()["detail"]


@pytest.mark.asyncio
async def test_send_message_reuses_shared_runner_without_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без override и без тёплого sandbox'а (ttl=0) сообщение обязано взять
    общий self._runner, а не построить новый TaskRunner.

    `_runner_for` реиспользует self._runner только когда `cfg is None and
    run_meta is None` (см. его docstring). Если send_message передаёт cfg
    безусловно (даже когда override пуст и cfg — тот же объект self.cfg по
    identity), эта ветка никогда не сработает: на каждое сообщение будет
    строиться свежий TaskRunner. С warm-сессиями (ttl>0, конфиг по
    умолчанию) это маскируется тёплым слотом, поэтому ttl=0 обязателен,
    чтобы тест дошёл до вызова _runner_for.
    """
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
        "cloud:\n  warm_session_ttl_sec: 0\n"
        f"storage:\n  db_path: {db_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    service = GatewayService(load_config(project_dir=ws), ws)

    captured: list[object] = []

    async def spy_run_bg(
        self: GatewayService,
        task: str,
        autonomy: object,
        started: asyncio.Future[str],
        *,
        runner: object = None,
        session_id: str | None = None,
        history: object = None,
        warm: object = None,
    ) -> None:
        captured.append(runner)
        started.set_result("stub-run-id")

    monkeypatch.setattr(GatewayService, "_run_bg", spy_run_bg)

    session = await service.create_session(title="без warm")
    await service.send_message(session.session_id, "задача", None)

    assert captured == [service._runner], "общий runner не переиспользован"
