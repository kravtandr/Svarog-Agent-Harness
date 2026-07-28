"""Каталог моделей провайдера (план 2026-07-28)."""

import httpx
import pytest

from svarog_harness.config.schema import ProviderConfig
from svarog_harness.gateway.catalog import CatalogError, fetch_models, parse_models


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
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    transport = httpx.MockTransport(handler)
    cards = await fetch_models(
        _provider("https://openrouter.ai/api/v1/"), "секрет", transport=transport
    )

    assert seen["url"] == "https://openrouter.ai/api/v1/models"
    assert seen["auth"] == "Bearer секрет"
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
