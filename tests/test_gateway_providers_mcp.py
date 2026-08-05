"""Настройки 31.07.2026: провайдеры, executor-дефолты, вкладка MCP."""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from svarog_harness.config.loader import USER_CONFIG_PATH, load_config
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayService:
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
        f"secrets:\n  path: {tmp_path / 'secrets.json'}\n"
        "executor:\n"
        "  type: external\n"
        "  external:\n"
        "    adapter: opencode\n"
        "    image: svarog/agent-opencode:latest\n"
        "    base_url: http://localhost:9\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    return GatewayService(load_config(project_dir=ws), ws)


@pytest.fixture
def client(service: GatewayService) -> TestClient:
    return TestClient(create_app(service))


def test_add_provider_writes_config_and_secret(
    client: TestClient, service: GatewayService, tmp_path: Path
) -> None:
    resp = client.post(
        "/models/providers",
        json={
            "name": "groq",
            "base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b",
            "api_key": "sk-секрет",
        },
    )
    assert resp.status_code == 200, resp.text
    data = yaml.safe_load(service.config_path.read_text(encoding="utf-8"))
    groq = data["models"]["providers"]["groq"]
    assert groq["base_url"] == "https://api.groq.com/openai/v1"
    assert groq["model"] == "llama-3.3-70b"
    assert groq["api_key_ref"] == "GROQ_API_KEY"
    # Ключ — в SecretStore, не в yaml (ADR-0006).
    assert "sk-секрет" not in service.config_path.read_text(encoding="utf-8")
    secrets = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert secrets["GROQ_API_KEY"] == "sk-секрет"
    # Конфиг перечитан: провайдер виден в /models.
    names = [p["name"] for p in client.get("/models").json()]
    assert "groq" in names


def test_add_provider_rejects_bad_input(client: TestClient) -> None:
    assert (
        client.post(
            "/models/providers",
            json={"name": "плохое имя", "base_url": "https://x", "model": "m"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/models/providers",
            json={"name": "ok", "base_url": "ftp://x", "model": "m"},
        ).status_code
        == 422
    )


def test_add_provider_in_workspace_without_own_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проектный svarog.yaml частичен или отсутствует — правка обязана пройти.

    Полный конфиг живёт на user-уровне (~/.svarog/svarog.yaml), load_config
    мержит проектный поверх. До фикса 31.07.2026 _write_deep валидировал
    фрагмент в одиночку и падал «Field required» на models.default.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    user_cfg = tmp_path / ".svarog" / "svarog.yaml"
    user_cfg.parent.mkdir(parents=True)
    user_cfg.write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: docker\n"
        f"secrets:\n  path: {tmp_path / 'secrets.json'}\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    ws = tmp_path / "desktop"
    ws.mkdir()  # своего svarog.yaml нет — как папка, открытая через пикер
    svc = GatewayService(load_config(project_dir=ws), ws)
    app_client = TestClient(create_app(svc))

    resp = app_client.post(
        "/models/providers",
        json={
            "name": "LiteLLM",
            "base_url": "https://litellm.example:9443/v1",
            "model": "qwen3-32b",
            "api_key": "sk-lite",
        },
    )
    assert resp.status_code == 200, resp.text
    # Проектный файл создан и остался частичным: только новый провайдер.
    written = yaml.safe_load(svc.config_path.read_text(encoding="utf-8"))
    assert written["models"]["providers"]["LiteLLM"]["model"] == "qwen3-32b"
    assert "default" not in written["models"]
    # Эффективный конфиг видит обоих провайдеров.
    names = [p["name"] for p in app_client.get("/models").json()]
    assert sorted(names) == ["LiteLLM", "local"]


def test_broken_user_config_gives_422_not_500(client: TestClient, tmp_path: Path) -> None:
    """~/.svarog/svarog.yaml сломали после старта serve — правка отвечает 422.

    _validate_effective читает user-файл на каждую правку; кривой YAML в нём
    не должен ронять запрос в 500 из недр парсера.
    """
    user_cfg = tmp_path / ".svarog" / "svarog.yaml"
    user_cfg.parent.mkdir(parents=True, exist_ok=True)
    user_cfg.write_text("models: [это, не, mapping\n", encoding="utf-8")

    resp = client.post(
        "/models/providers",
        json={"name": "lite", "base_url": "https://x/v1", "model": "m"},
    )
    assert resp.status_code == 422
    assert "не читается" in resp.json()["detail"]


def test_scan_models_returns_catalog_of_unsaved_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from svarog_harness.gateway.catalog import CatalogError, ModelCard

    seen: dict[str, object] = {}

    async def fake_fetch(provider, api_key, **kw):
        seen["base_url"] = provider.base_url
        seen["api_key"] = api_key
        return [ModelCard(id="qwen3-32b", name="Qwen3 32B", context_length=32768)]

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", fake_fetch)
    resp = client.post(
        "/models/scan",
        json={"base_url": "https://litellm.example:9443/v1", "api_key": "sk-x"},
    )
    assert resp.status_code == 200, resp.text
    assert [card["id"] for card in resp.json()] == ["qwen3-32b"]
    assert seen == {"base_url": "https://litellm.example:9443/v1", "api_key": "sk-x"}

    async def broken_fetch(provider, api_key, **kw):
        raise CatalogError("провайдер ответил 401")

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", broken_fetch)
    resp = client.post("/models/scan", json={"base_url": "https://litellm.example:9443/v1"})
    assert resp.status_code == 502
    assert "401" in resp.json()["detail"]


def test_executor_defaults_native_switches_provider_and_model(
    client: TestClient, service: GatewayService
) -> None:
    client.post(
        "/models/providers",
        json={"name": "groq", "base_url": "https://api.groq.com/openai/v1", "model": "ll"},
    )
    resp = client.post(
        "/executors/defaults",
        json={"executor": "native", "provider": "groq", "model": "llama-3.3-70b"},
    )
    assert resp.status_code == 200, resp.text
    data = yaml.safe_load(service.config_path.read_text(encoding="utf-8"))
    assert data["models"]["default"] == "groq"
    assert data["models"]["providers"]["groq"]["model"] == "llama-3.3-70b"


def test_executor_defaults_opencode_derives_provider(
    client: TestClient, service: GatewayService
) -> None:
    resp = client.post(
        "/executors/defaults",
        json={"executor": "opencode", "provider": "local", "model": "z-ai/glm-5.2"},
    )
    assert resp.status_code == 200, resp.text
    data = yaml.safe_load(service.config_path.read_text(encoding="utf-8"))
    ext = data["executor"]["external"]
    assert ext["model"] == "z-ai/glm-5.2"
    assert ext["base_url"] == "http://localhost:9"  # /v1 срезан из карточки
    assert ext["auth"] == "api-key"


def test_executor_defaults_claude_model_only(client: TestClient, service: GatewayService) -> None:
    resp = client.post("/executors/defaults", json={"executor": "claude-code", "model": "opus"})
    assert resp.status_code == 200, resp.text
    data = yaml.safe_load(service.config_path.read_text(encoding="utf-8"))
    assert data["executor"]["external"]["model"] == "opus"
    # Провайдер для claude-code — отказ: у него своя подписка.
    assert (
        client.post(
            "/executors/defaults", json={"executor": "claude-code", "provider": "local"}
        ).status_code
        == 422
    )


def test_mcp_add_list_remove(client: TestClient, service: GatewayService) -> None:
    resp = client.post(
        "/mcp",
        json={
            "name": "fetch",
            "command": "uvx",
            "args": ["mcp-server-fetch"],
            "risk": "medium",
        },
    )
    assert resp.status_code == 200, resp.text
    listed = client.get("/mcp").json()
    assert [s["name"] for s in listed] == ["fetch"]
    assert listed[0]["command"] == "uvx"
    assert listed[0]["args"] == ["mcp-server-fetch"]
    assert listed[0]["risk"] == "medium"

    assert client.delete("/mcp/fetch").status_code == 200
    assert client.get("/mcp").json() == []
    assert client.delete("/mcp/fetch").status_code == 404


def test_mcp_test_reports_failure_honestly(client: TestClient) -> None:
    resp = client.post("/mcp/test", json={"command": "no-such-binary-абв", "args": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]


def test_mcp_test_survives_server_that_starts_but_does_not_speak_mcp(
    client: TestClient,
) -> None:
    """Отказ после открытия транспорта — данные ответа, а не сорванный запрос.

    Несуществующий бинарь падает до того, как транспорт открылся, поэтому эту
    ветку он не проверяет. `true` стартует успешно и сразу выходит: раньше
    открытый stdio-транспорт оставался незакрытым, а сборщик мусора разбирал
    его в чужой задаче — «Attempted to exit cancel scope in a different task».
    """
    resp = client.post("/mcp/test", json={"command": "true", "args": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]
    assert body["tools"] == []


def test_provider_check_reports_state_honestly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверка доступности: ок и недоступность — данные ответа, не исключение."""
    from svarog_harness.gateway.catalog import CatalogError, ModelCard

    async def fake_fetch(provider, api_key, **kw):
        return [ModelCard(id="a"), ModelCard(id="b")]

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", fake_fetch)
    resp = client.post("/models/providers/local/check")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "models_count": 2, "error": None}

    async def broken_fetch(provider, api_key, **kw):
        raise CatalogError("провайдер ответил 401")

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", broken_fetch)
    resp = client.post("/models/providers/local/check")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "401" in body["error"]

    assert client.post("/models/providers/нет-такого/check").status_code == 404


def test_provider_check_bypasses_negative_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Проверить» обязан отражать состояние сейчас, а не отрицательный кэш."""
    from svarog_harness.gateway.catalog import CatalogError, ModelCard

    async def broken_fetch(provider, api_key, **kw):
        raise CatalogError("connect timeout")

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", broken_fetch)
    # Проваленный обычный запрос каталога кладёт неудачу в кэш.
    assert client.get("/models/local").status_code == 502

    async def fake_fetch(provider, api_key, **kw):
        return [ModelCard(id="a")]

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", fake_fetch)
    resp = client.post("/models/providers/local/check")
    assert resp.json() == {"ok": True, "models_count": 1, "error": None}


def test_provider_rename_moves_fields_and_default(
    client: TestClient, service: GatewayService
) -> None:
    """Rename переносит поля и default; api_key_ref остаётся валидным (ADR-0006)."""
    client.post(
        "/models/providers",
        json={
            "name": "local",
            "base_url": "https://openrouter.ai/api/v1",
            "model": "deepseek/deepseek-v4-flash",
            "api_key": "sk-or-секрет",
        },
    )
    resp = client.post("/models/providers/local/rename", json={"new_name": "openrouter"})
    assert resp.status_code == 200, resp.text
    data = yaml.safe_load(service.config_path.read_text(encoding="utf-8"))
    moved = data["models"]["providers"]["openrouter"]
    assert moved["base_url"] == "https://openrouter.ai/api/v1"
    assert moved["model"] == "deepseek/deepseek-v4-flash"
    # Секрет не перевводится: ссылка переезжает как есть.
    assert moved["api_key_ref"] == "LOCAL_API_KEY"
    assert "local" not in data["models"]["providers"]
    assert data["models"]["default"] == "openrouter"
    names = [p["name"] for p in client.get("/models").json()]
    assert names == ["openrouter"]


def test_provider_rename_rejects_bad_targets(client: TestClient) -> None:
    client.post(
        "/models/providers",
        json={"name": "groq", "base_url": "https://api.groq.com/openai/v1", "model": "ll"},
    )
    # Занятое имя, кривое имя, неизвестный источник.
    assert (
        client.post("/models/providers/local/rename", json={"new_name": "groq"}).status_code == 422
    )
    assert (
        client.post("/models/providers/local/rename", json={"new_name": "плохое"}).status_code
        == 422
    )
    assert client.post("/models/providers/нет/rename", json={"new_name": "ok"}).status_code == 404


def test_provider_rename_updates_auxiliary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rename обновляет models.auxiliary, если он указывал на переименовываемый провайдер."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  auxiliary: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: docker\n"
        f"secrets:\n  path: {tmp_path / 'secrets.json'}\n"
        "executor:\n"
        "  type: external\n"
        "  external:\n"
        "    adapter: opencode\n"
        "    image: svarog/agent-opencode:latest\n"
        "    base_url: http://localhost:9\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    svc = GatewayService(load_config(project_dir=ws), ws)
    app_client = TestClient(create_app(svc))

    # Переименовать провайдер, который установлен как auxiliary.
    resp = app_client.post("/models/providers/local/rename", json={"new_name": "cheap"})
    assert resp.status_code == 200, resp.text
    data = yaml.safe_load(svc.config_path.read_text(encoding="utf-8"))
    # auxiliary должен указывать на новое имя.
    assert data["models"]["auxiliary"] == "cheap"
    assert data["models"]["default"] == "cheap"
    assert "local" not in data["models"]["providers"]


def test_provider_remove_guards_default(client: TestClient, service: GatewayService) -> None:
    """Remove отказывает, если удаляемый провайдер — default."""
    client.post(
        "/models/providers",
        json={"name": "groq", "base_url": "https://api.groq.com/openai/v1", "model": "ll"},
    )
    # Дефолтного удалять нельзя — сначала переключить.
    assert client.delete("/models/providers/local").status_code == 422
    client.post("/executors/defaults", json={"executor": "native", "provider": "groq"})
    assert client.delete("/models/providers/local").status_code == 200
    names = [p["name"] for p in client.get("/models").json()]
    assert names == ["groq"]
    assert client.delete("/models/providers/local").status_code == 404


def test_provider_remove_guards_auxiliary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove отказывает, если удаляемый провайдер — auxiliary (не default)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: main\n"
        "  auxiliary: cheap\n"
        "  providers:\n"
        "    main:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "    cheap:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: cheap-model\n"
        "sandbox:\n  type: docker\n"
        f"secrets:\n  path: {tmp_path / 'secrets.json'}\n"
        "executor:\n"
        "  type: external\n"
        "  external:\n"
        "    adapter: opencode\n"
        "    image: svarog/agent-opencode:latest\n"
        "    base_url: http://localhost:9\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    svc = GatewayService(load_config(project_dir=ws), ws)
    app_client = TestClient(create_app(svc))

    # Удалять auxiliary нельзя, даже если он не default.
    resp = app_client.delete("/models/providers/cheap")
    assert resp.status_code == 422
    # Проверяем, что сообщение об ошибке специфично для auxiliary.
    assert "auxiliary" in resp.json()["detail"]


def test_provider_remove_collapses_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove удаляет последнего провайдера и сворачивает пустую обёртку models."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # User config с дефолтным провайдером.
    user_cfg = tmp_path / ".svarog" / "svarog.yaml"
    user_cfg.parent.mkdir(parents=True)
    user_cfg.write_text(
        "models:\n"
        "  default: user-provider\n"
        "  providers:\n"
        "    user-provider:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: docker\n"
        f"secrets:\n  path: {tmp_path / 'secrets.json'}\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    # Project config с одним провайдером (не дефолтным, чтобы избежать guard).
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  providers:\n"
        "    project-only:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: project-model\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    svc = GatewayService(load_config(project_dir=ws), ws)
    app_client = TestClient(create_app(svc))

    # DELETE последнего провайдера проектного файла.
    resp = app_client.delete("/models/providers/project-only")
    assert resp.status_code == 200
    # Проектный файл больше не содержит models (обёртка свёрнута).
    proj_data = yaml.safe_load(svc.config_path.read_text(encoding="utf-8"))
    assert "models" not in proj_data
    # Эффективный конфиг всё ещё видит пользовательский провайдер.
    names = [p["name"] for p in app_client.get("/models").json()]
    assert names == ["user-provider"]


def test_provider_remove_user_config_only_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remove отказывает, если провайдер только в user config, не в project file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # User config с несколькими провайдерами.
    user_cfg = tmp_path / ".svarog" / "svarog.yaml"
    user_cfg.parent.mkdir(parents=True)
    user_cfg.write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "    user-only:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: user-model\n"
        "sandbox:\n  type: docker\n"
        f"secrets:\n  path: {tmp_path / 'secrets.json'}\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    # Project config без провайдеров.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    svc = GatewayService(load_config(project_dir=ws), ws)
    app_client = TestClient(create_app(svc))

    # DELETE провайдера, который только в user config — должна быть ошибка.
    resp = app_client.delete("/models/providers/user-only")
    assert resp.status_code == 422
    assert "~/.svarog" in resp.json()["detail"]


def test_provider_rename_user_config_only_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rename отказывает, если провайдер только в user config, не в project file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # User config с несколькими провайдерами.
    user_cfg = tmp_path / ".svarog" / "svarog.yaml"
    user_cfg.parent.mkdir(parents=True)
    user_cfg.write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "    user-only:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: user-model\n"
        "sandbox:\n  type: docker\n"
        f"secrets:\n  path: {tmp_path / 'secrets.json'}\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    # Project config без провайдеров.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    svc = GatewayService(load_config(project_dir=ws), ws)
    app_client = TestClient(create_app(svc))

    # POST rename провайдера, который только в user config — должна быть ошибка.
    resp = app_client.post("/models/providers/user-only/rename", json={"new_name": "user-renamed"})
    assert resp.status_code == 422
    assert "~/.svarog" in resp.json()["detail"]


async def _noop() -> AsyncIterator[None]:  # pragma: no cover — заглушка типов
    yield


@pytest.fixture
def global_service(service: GatewayService) -> GatewayService:
    """Сервис так, как его строят WorkspaceHub и локальный serve.

    user_config_path — разрешение писать MCP в ~/.svarog/svarog.yaml, то есть
    глобально для всех рабочих папок. TenantHub его не ставит: этот файл
    принадлежит оператору хоста.
    """
    service.user_config_path = USER_CONFIG_PATH.expanduser()
    return service


@pytest.fixture
def global_client(global_service: GatewayService) -> TestClient:
    return TestClient(create_app(global_service))


def test_mcp_add_writes_globally_not_into_project(
    global_client: TestClient, global_service: GatewayService, tmp_path: Path
) -> None:
    """Новый сервер уходит в пользовательский слой — он один на все папки."""
    resp = global_client.post(
        "/mcp",
        json={"name": "memory", "command": "npx", "args": ["-y", "srv"], "risk": "low"},
    )
    assert resp.status_code == 200, resp.text

    user_raw = yaml.safe_load((tmp_path / ".svarog" / "svarog.yaml").read_text())
    assert user_raw["mcp"]["servers"]["memory"]["command"] == "npx"

    project_raw = yaml.safe_load(global_service.config_path.read_text())
    assert "mcp" not in (project_raw or {}), "проектный файл трогать не должны"


def test_mcp_add_stays_in_project_without_global_scope(
    client: TestClient, service: GatewayService, tmp_path: Path
) -> None:
    """Без user_config_path (режим тенанта) запись идёт в конфиг тенанта.

    ~/.svarog/svarog.yaml в multi-tenant принадлежит оператору хоста: запись
    туда подняла бы серверы одного жильца всем остальным.
    """
    resp = client.post(
        "/mcp",
        json={"name": "memory", "command": "npx", "args": [], "risk": "low"},
    )
    assert resp.status_code == 200, resp.text
    project_raw = yaml.safe_load(service.config_path.read_text())
    assert project_raw["mcp"]["servers"]["memory"]["command"] == "npx"
    assert not (tmp_path / ".svarog" / "svarog.yaml").exists()


def test_mcp_list_tells_where_each_server_lives(
    global_client: TestClient, global_service: GatewayService, tmp_path: Path
) -> None:
    """Список показывает действующий набор и происхождение каждой записи.

    Показывать только глобальные значило бы врать: проектные тоже попадают в
    запуск через merge, и человек не понимал бы, откуда у агента инструменты.
    """
    user_dir = tmp_path / ".svarog"
    user_dir.mkdir(exist_ok=True)
    (user_dir / "svarog.yaml").write_text(
        "mcp:\n  servers:\n    глобальный:\n      command: npx\n      risk: low\n",
        encoding="utf-8",
    )
    project = yaml.safe_load(global_service.config_path.read_text())
    project["mcp"] = {"servers": {"проектный": {"command": "uvx", "risk": "high"}}}
    global_service.config_path.write_text(yaml.safe_dump(project), encoding="utf-8")
    global_service.cfg = load_config(project_dir=global_service.workspace)

    body = global_client.get("/mcp").json()
    scopes = {item["name"]: item["scope"] for item in body}
    assert scopes == {"глобальный": "user", "проектный": "project"}


def test_mcp_remove_targets_the_file_that_holds_it(
    global_client: TestClient, global_service: GatewayService, tmp_path: Path
) -> None:
    """Удаление правит тот файл, где запись лежит, а не всегда проектный."""
    user_dir = tmp_path / ".svarog"
    user_dir.mkdir(exist_ok=True)
    user_file = user_dir / "svarog.yaml"
    user_file.write_text(
        "mcp:\n  servers:\n    глобальный:\n      command: npx\n      risk: low\n",
        encoding="utf-8",
    )
    global_service.cfg = load_config(project_dir=global_service.workspace)

    assert global_client.delete("/mcp/глобальный").status_code == 200
    # Последний сервер уносит с собой и обёртку mcp: пустая `servers:` парсится
    # в None и валит валидацию. Проверяем суть — записи в файле больше нет.
    user_raw = yaml.safe_load(user_file.read_text()) or {}
    assert "глобальный" not in (user_raw.get("mcp") or {}).get("servers", {})
    assert global_client.get("/mcp").json() == []
