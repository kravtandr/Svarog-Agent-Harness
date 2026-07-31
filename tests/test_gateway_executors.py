"""Варианты исполнителя для селекта поля ввода (план 2026-07-28)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app
from svarog_harness.gateway.executors import executor_options
from svarog_harness.gateway.overrides import (
    OVERRIDE_META_KEY,
    OverrideError,
    RunOverride,
    apply_override,
)
from svarog_harness.runtime.agents import (
    ADAPTER_BINARIES,
    EXTERNAL_ADAPTERS,
    adapter_available,
)
from svarog_harness.sandbox.base import ExecResult
from svarog_harness.sandbox.docker import DockerEnvironment
from svarog_harness.scaffold import DEFAULT_CLAUDE_IMAGE, DEFAULT_OPENCODE_IMAGE
from svarog_harness.trace.lookup import find_run_by_prefix


def test_registry_lists_every_adapter_with_its_binary() -> None:
    assert EXTERNAL_ADAPTERS == ("claude-code", "codex", "opencode")
    assert set(ADAPTER_BINARIES) == set(EXTERNAL_ADAPTERS)
    assert ADAPTER_BINARIES["claude-code"] == "claude"


def test_availability_is_a_path_lookup(monkeypatch) -> None:
    monkeypatch.setattr(
        "svarog_harness.runtime.agents.shutil.which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )
    assert adapter_available("claude-code") is True
    assert adapter_available("codex") is False
    assert adapter_available("нет-такого") is False


def _config(tmp_path: Path, image: str = DEFAULT_CLAUDE_IMAGE) -> object:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: docker\n"
        "executor:\n"
        "  type: external\n"
        "  external:\n"
        "    adapter: claude-code\n"
        f"    image: {image}\n",
        encoding="utf-8",
    )
    return load_config(project_dir=ws, user_config_path=tmp_path / "нет")


def test_adapter_switches_adapter_and_default_image(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    derived = apply_override(cfg, RunOverride(executor="external", adapter="opencode"))
    assert derived.executor.external.adapter == "opencode"
    assert derived.executor.external.image == DEFAULT_OPENCODE_IMAGE


def test_custom_image_is_left_alone(tmp_path: Path) -> None:
    cfg = _config(tmp_path, image="registry.example/мой-агент:7")
    derived = apply_override(cfg, RunOverride(executor="external", adapter="opencode"))
    assert derived.executor.external.adapter == "opencode"
    assert derived.executor.external.image == "registry.example/мой-агент:7"


def test_codex_without_own_image_is_rejected(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(OverrideError, match="codex"):
        apply_override(cfg, RunOverride(executor="external", adapter="codex"))


def test_codex_allowed_when_image_is_custom(tmp_path: Path) -> None:
    cfg = _config(tmp_path, image="registry.example/codex:1")
    derived = apply_override(cfg, RunOverride(executor="external", adapter="codex"))
    assert derived.executor.external.adapter == "codex"


def test_adapter_without_external_executor_is_rejected(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(OverrideError, match="native"):
        apply_override(cfg, RunOverride(executor="native", adapter="opencode"))


def test_adapter_round_trips_through_meta() -> None:
    ov = RunOverride(executor="external", adapter="opencode")
    assert ov.to_meta() == {"executor": "external", "adapter": "opencode"}
    assert RunOverride.from_meta({"override": ov.to_meta()}) == ov
    assert RunOverride.from_meta({"override": {"adapter": 42}}).adapter is None


def test_unknown_adapter_is_rejected_even_with_custom_image(tmp_path: Path) -> None:
    """Регрессия: с кастомным образом ветка подмены образа не выполняется,
    и без отдельной проверки неизвестное имя адаптера уходило прямиком в
    model_copy — мимо любой валидации (pydantic не перепроверяет Literal
    при model_copy)."""
    cfg = _config(tmp_path, image="registry.example/custom:1")
    with pytest.raises(OverrideError, match="totally-bogus-adapter"):
        apply_override(cfg, RunOverride(executor="external", adapter="totally-bogus-adapter"))


def test_adapter_only_override_on_already_external_config(tmp_path: Path) -> None:
    """executor не задан в override — исполнитель берётся из cfg (уже
    external), но адаптер и образ всё равно должны подмениться."""
    cfg = _config(tmp_path)
    derived = apply_override(cfg, RunOverride(adapter="opencode"))
    assert derived.executor.external.adapter == "opencode"
    assert derived.executor.external.image == DEFAULT_OPENCODE_IMAGE


# --- список исполнителей для селекта (задача 3) ---------------------------


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayService:
    monkeypatch.setenv("HOME", str(tmp_path))

    # Образы агентов (svarog/agent-*) собирает локально `svarog init`, в
    # реестре их нет. Отправка сообщения поднимает sandbox синхронно
    # (service._acquire_warm → prepare_session_resources), поэтому на машине
    # без готового образа `docker run` отвечал pull access denied, а запрос —
    # 422 вместо 201. Тесты здесь про то, что выбор в композере доезжает до
    # Run.meta и производного конфига, а не про исполнение внутри контейнера:
    # подменяем старт контейнера, оставляя весь остальной путь настоящим.
    async def stub_start(self: DockerEnvironment) -> None:
        self._docker = "stub-docker"
        self._container_id = "stub-container"

    async def stub_execute(
        self: DockerEnvironment, command: str, *, timeout_sec: float
    ) -> ExecResult:
        return ExecResult(exit_code=1, stdout="", stderr="sandbox застаблен в тесте")

    async def stub_cleanup(self: DockerEnvironment) -> None:
        self._container_id = None

    monkeypatch.setattr(DockerEnvironment, "start", stub_start)
    monkeypatch.setattr(DockerEnvironment, "execute", stub_execute)
    monkeypatch.setattr(DockerEnvironment, "cleanup", stub_cleanup)
    cfg = _config(tmp_path)
    return GatewayService(cfg, tmp_path / "ws")


def test_native_always_present_and_active_matches_config(tmp_path: Path) -> None:
    cfg = _config(tmp_path)  # executor.type = external, adapter = claude-code
    options = executor_options(cfg)
    by_value = {o.value: o for o in options}
    assert by_value["native"].kind == "native"
    assert by_value["native"].available is True
    assert by_value["claude-code"].is_active is True
    assert by_value["native"].is_active is False


def test_configured_adapter_is_available_even_without_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("svarog_harness.gateway.executors.adapter_available", lambda _: False)
    monkeypatch.setattr("svarog_harness.gateway.executors._image_present", lambda _: False)
    options = {o.value: o for o in executor_options(_config(tmp_path))}
    assert options["claude-code"].available is True, "прописан в конфиге"
    assert options["opencode"].available is False
    assert "codex" in options, "недоступный адаптер показывается, а не прячется"


def test_adapter_available_via_local_docker_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """31.07.2026: собранный svarog/agent-opencode:latest показывался
    недоступным только потому, что host-CLI opencode не стоял — а исполняется
    адаптер В КОНТЕЙНЕРЕ (ADR-0016), host-CLI ему не нужен."""
    monkeypatch.setattr("svarog_harness.gateway.executors.adapter_available", lambda _: False)
    monkeypatch.setattr(
        "svarog_harness.gateway.executors._image_present",
        lambda image: image == DEFAULT_OPENCODE_IMAGE,
    )
    options = {o.value: o for o in executor_options(_config(tmp_path))}
    assert options["opencode"].available is True, "образ собран локально"
    assert options["codex"].available is False, "у codex нет ни CLI, ни образа"


def test_sandbox_options_reflect_docker_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from svarog_harness.gateway.executors import sandbox_options

    monkeypatch.setattr("svarog_harness.gateway.executors.find_docker", lambda: "/usr/bin/docker")
    options = {o.value: o for o in sandbox_options(_config(tmp_path))}
    assert options["docker"].available is True
    assert options["docker"].is_active is True  # конфиг фикстуры — docker
    assert options["local-trusted"].available is True
    assert options["local-trusted"].is_active is False

    monkeypatch.setattr("svarog_harness.gateway.executors.find_docker", lambda: None)
    options = {o.value: o for o in sandbox_options(_config(tmp_path))}
    assert options["docker"].available is False, "runtime нет — но вариант виден"


def test_executors_endpoint(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))
    body = client.get("/executors").json()
    assert body[0]["value"] == "native"
    assert all("available" in o and "is_active" in o for o in body)


def test_sandboxes_endpoint(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))
    body = client.get("/sandboxes").json()
    assert [o["value"] for o in body] == ["docker", "local-trusted"]
    assert all("available" in o and "is_active" in o for o in body)


def test_sandbox_override_switches_type_and_round_trips(tmp_path: Path) -> None:
    """sandbox — свойство сообщения: docker-конфиг + override local-trusted
    (native) даёт производный конфиг с local-trusted, meta восстанавливается."""
    cfg = _config(tmp_path)
    ov = RunOverride(executor="native", sandbox="local-trusted")
    derived = apply_override(cfg, ov)
    assert derived.sandbox.type == "local-trusted"
    assert cfg.sandbox.type == "docker"  # исходный конфиг не мутируется
    restored = RunOverride.from_meta({OVERRIDE_META_KEY: ov.to_meta()})
    assert restored.sandbox == "local-trusted"


def test_sandbox_local_trusted_with_external_is_rejected(tmp_path: Path) -> None:
    """ADR-0016 по эффективной паре: и «local-trusted при external-конфиге»,
    и «external при local-trusted» — отказ, а не тихая несовместимость."""
    cfg = _config(tmp_path)  # executor external
    with pytest.raises(OverrideError, match="docker"):
        apply_override(cfg, RunOverride(sandbox="local-trusted"))
    with pytest.raises(OverrideError, match="docker"):
        apply_override(cfg, RunOverride(adapter="opencode", sandbox="local-trusted"))


# --- adapter из composer'а должен доходить до запуска (2026-07-27) --------


@pytest.mark.asyncio
async def test_message_adapter_reaches_run_meta_and_config(
    service: GatewayService,
) -> None:
    """Выбор 'opencode' в композере — это RunOverride.adapter, а не только
    executor='external'. Фикстура `_config` держит образ claude-code (это
    известный дефолт `svarog init`), так что производный конфиг обязан
    подхватить и адаптер, и его дефолтный образ."""
    session = await service.create_session(title="adapter из composer'а")
    client = TestClient(create_app(service=service))

    response = client.post(
        f"/sessions/{session.session_id}/messages",
        json={"text": "задача", "executor": "external", "adapter": "opencode"},
    )
    assert response.status_code == 201
    run_id = response.json()["run_id"]

    async def read(db):
        run = await find_run_by_prefix(db, run_id)
        return dict(run.meta or {})

    meta = await service._read(read)
    assert meta[OVERRIDE_META_KEY] == {"executor": "external", "adapter": "opencode"}

    runner = await service._runner_for_run(run_id)
    assert runner.cfg.executor.external.adapter == "opencode"
    assert runner.cfg.executor.external.image == DEFAULT_OPENCODE_IMAGE


@pytest.mark.asyncio
async def test_message_adapter_with_native_executor_returns_422(
    service: GatewayService,
) -> None:
    """Адаптер и native-исполнитель несовместимы (apply_override это уже
    проверяет) — HTTP-путь обязан довести отказ до 422, а не 500."""
    session = await service.create_session(title="adapter+native")
    client = TestClient(create_app(service=service))

    response = client.post(
        f"/sessions/{session.session_id}/messages",
        json={"text": "задача", "executor": "native", "adapter": "opencode"},
    )
    assert response.status_code == 422
    assert "native" in response.json()["detail"]
