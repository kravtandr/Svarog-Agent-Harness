"""Каталог моделей провайдера (план 2026-07-28)."""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import ProviderConfig
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app
from svarog_harness.gateway.catalog import CatalogError, ModelCard, fetch_models, parse_models
from svarog_harness.gateway.overrides import RunOverride
from svarog_harness.runtime.config_snapshot import CONFIG_HASH_META_KEY, config_digest
from svarog_harness.trace.lookup import find_run_by_prefix


def test_parses_openrouter_shape_with_pricing() -> None:
    cards = parse_models(
        {
            "data": [
                {
                    "id": "deepseek/deepseek-v4-flash",
                    "name": "DeepSeek V4 Flash",
                    "context_length": 163840,
                    # OpenRouter отдаёт цену за один токен строкой.
                    "pricing": {"prompt": "0.0000005", "completion": "0.0000015"},
                }
            ]
        }
    )
    assert len(cards) == 1
    assert cards[0].id == "deepseek/deepseek-v4-flash"
    assert cards[0].name == "DeepSeek V4 Flash"
    assert cards[0].context_length == 163840
    assert cards[0].input_usd_per_mtok == pytest.approx(0.5)
    assert cards[0].output_usd_per_mtok == pytest.approx(1.5)


def test_parses_bare_openai_shape() -> None:
    cards = parse_models({"data": [{"id": "gpt-5", "object": "model"}]})
    assert cards == [
        type(cards[0])(
            id="gpt-5",
            name=None,
            context_length=None,
            input_usd_per_mtok=None,
            output_usd_per_mtok=None,
        )
    ]


def test_skips_entries_without_id_instead_of_failing() -> None:
    cards = parse_models({"data": [{"name": "без id"}, {"id": "ok"}, "мусор"]})
    assert [c.id for c in cards] == ["ok"]


def test_garbage_payload_gives_empty_list() -> None:
    assert parse_models({"data": "не список"}) == []
    assert parse_models({}) == []


def _provider(base_url: str) -> ProviderConfig:
    return ProviderConfig(base_url=base_url, model="fake")


@pytest.mark.asyncio
async def test_fetch_uses_base_url_as_is_and_sends_key() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        # Проверяем, что заголовок Host не был удален (ADR-0006 compliance).
        assert request.headers.get("host") is not None, "Host header is mandatory"
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    transport = httpx.MockTransport(handler)
    cards = await fetch_models(
        _provider("https://openrouter.ai/api/v1/"), "sk-test-secret", transport=transport
    )

    assert seen["url"] == "https://openrouter.ai/api/v1/models"
    assert seen["auth"] == "Bearer sk-test-secret"
    assert [c.id for c in cards] == ["m1"]


@pytest.mark.asyncio
async def test_fetch_without_key_sends_no_auth_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") is None
        return httpx.Response(200, json={"data": []})

    await fetch_models(
        _provider("http://localhost:9/v1"), None, transport=httpx.MockTransport(handler)
    )


@pytest.mark.asyncio
async def test_http_error_becomes_catalog_error_with_status() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(401, text="no key"))
    with pytest.raises(CatalogError, match="401"):
        await fetch_models(_provider("https://x/v1"), None, transport=transport)


@pytest.mark.asyncio
async def test_non_json_becomes_catalog_error() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text="<html>"))
    with pytest.raises(CatalogError, match="не JSON"):
        await fetch_models(_provider("https://x/v1"), None, transport=transport)


@pytest.mark.asyncio
async def test_network_failure_becomes_catalog_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет связи")

    with pytest.raises(CatalogError, match="нет связи"):
        await fetch_models(_provider("https://x/v1"), None, transport=httpx.MockTransport(handler))


# --- эндпоинты, кэш и цены (задача 6) --------------------------------------


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
        "cloud:\n  warm_session_ttl_sec: 60\n"
        f"storage:\n  db_path: {db_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    return GatewayService(load_config(project_dir=ws), ws)


@pytest.mark.asyncio
async def test_providers_endpoint_lists_config_entries(service) -> None:
    client = TestClient(create_app(service=service))
    body = client.get("/models").json()
    assert [p["name"] for p in body] == ["local", "router"]
    assert [p["is_default"] for p in body] == [True, False]
    assert body[0]["model"] == "fake-model"


@pytest.mark.asyncio
async def test_models_endpoint_caches_second_call(service, monkeypatch) -> None:
    calls = {"n": 0}

    async def fake_fetch(provider, api_key, **kwargs):
        calls["n"] += 1
        return [ModelCard(id="m1", name="M1")]

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", fake_fetch)
    client = TestClient(create_app(service=service))

    first = client.get("/models/router")
    second = client.get("/models/router")

    assert first.status_code == 200
    assert [m["id"] for m in first.json()] == ["m1"]
    assert second.json() == first.json()
    assert calls["n"] == 1, "второй вызов обслужен кэшем"


@pytest.mark.asyncio
async def test_unknown_provider_is_404(service) -> None:
    client = TestClient(create_app(service=service))
    assert client.get("/models/нет-такого").status_code == 404


@pytest.mark.asyncio
async def test_provider_failure_is_502_with_reason(service, monkeypatch) -> None:
    async def boom(provider, api_key, **kwargs):
        raise CatalogError("https://x/models: провайдер ответил 401")

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", boom)
    client = TestClient(create_app(service=service))
    response = client.get("/models/router")
    assert response.status_code == 502
    assert "401" in response.json()["detail"]


@pytest.mark.asyncio
async def test_model_override_takes_prices_from_catalog(service, monkeypatch) -> None:
    async def fake_fetch(provider, api_key, **kwargs):
        return [ModelCard(id="x/y", input_usd_per_mtok=0.25, output_usd_per_mtok=0.75)]

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", fake_fetch)
    session = await service.create_session(title="цены")
    run_id = await service.send_message(
        session.session_id, "задача", None, RunOverride(provider="router", model="x/y")
    )
    runner = await service._runner_for_run(run_id)
    # При resume цены восстанавливаются из того же каталога.
    assert runner.cfg.models.providers["router"].input_usd_per_mtok == 0.25


@pytest.mark.asyncio
async def test_warm_session_start_and_resume_agree_on_priced_config(service, monkeypatch) -> None:
    """Тёплая сессия (fixture: cloud.warm_session_ttl_sec=60) не должна создавать
    дрейф конфига между стартом run'а и его resume.

    Регрессия: `_acquire_warm` строил производный конфиг голым `apply_override`
    (без цен), тогда как `send_message`/`_runner_for_run` уже шли через `_derive`.
    Когда тёплый слот создаётся заново под override с моделью, реальный запуск
    исполнялся под непроцененным конфигом, а `config_hash` в Run.meta писался
    именно от него; resume пересчитывал цену заново через `_derive` и получал
    другой дайджест — `_assert_config_unchanged` отклонил бы такой resume
    (ADR-0015 §0.4). Проверяем и то, что реально стартовавший runner несёт цены
    каталога, и то, что дайджест конфига, который соберёт resume, совпадает с
    тем, что записан при старте.
    """

    async def fake_fetch(provider, api_key, **kwargs):
        return [ModelCard(id="x/y", input_usd_per_mtok=0.25, output_usd_per_mtok=0.75)]

    monkeypatch.setattr("svarog_harness.gateway.service.fetch_models", fake_fetch)

    captured: list[object] = []
    orig_run_bg = GatewayService._run_bg

    async def spy_run_bg(self, task, autonomy, started, **kwargs):
        captured.append(kwargs.get("runner"))
        await orig_run_bg(self, task, autonomy, started, **kwargs)

    monkeypatch.setattr(GatewayService, "_run_bg", spy_run_bg)

    session = await service.create_session(title="тёплая сессия с ценами")
    run_id = await service.send_message(
        session.session_id, "задача", None, RunOverride(provider="router", model="x/y")
    )

    assert captured and captured[0] is not None, "runner не передан в _run_bg"
    started_runner = captured[0]
    # Runner, под которым реально стартовал run, обязан нести цены каталога —
    # а не цены из svarog.yaml для другой модели того же провайдера.
    assert started_runner.cfg.models.providers["router"].input_usd_per_mtok == 0.25
    assert started_runner.cfg.models.providers["router"].output_usd_per_mtok == 0.75

    async def read(db):
        run = await find_run_by_prefix(db, run_id)
        return dict(run.meta or {})

    meta = await service._read(read)
    resumed_runner = await service._runner_for_run(run_id)
    # Инвариант, который и защищает ADR-0015 §0.4: то, что пересчитает resume,
    # обязано дать тот же дайджест, что записан при реальном старте run'а.
    assert config_digest(resumed_runner.cfg, service.workspace) == meta[CONFIG_HASH_META_KEY]
