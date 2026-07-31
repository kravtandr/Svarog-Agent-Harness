"""Глубокий yaml-патчер настроек (провайдеры/MCP/executor-дефолты, 31.07.2026).

Нельзя пересобирать файл через safe_dump: svarog.yaml человек ведёт руками,
комментарии и порядок — часть файла. Патчер правит/вставляет/удаляет ровно
нужные строки.
"""

import yaml

from svarog_harness.gateway.settings import remove_deep_key, set_deep_value, set_deep_values

_BASE = """\
models:
  default: local  # активный провайдер
  providers:
    local:
      base_url: https://openrouter.ai/api/v1
      model: deepseek/deepseek-chat
      api_key_ref: PROVIDER_API_KEY

sandbox:
  type: docker
"""


def test_set_existing_nested_scalar_keeps_comment_and_rest() -> None:
    out = set_deep_value(_BASE, "models.providers.local.model", "z-ai/glm-5.2")
    data = yaml.safe_load(out)
    assert data["models"]["providers"]["local"]["model"] == "z-ai/glm-5.2"
    assert "# активный провайдер" in out  # комментарии живы
    assert data["sandbox"]["type"] == "docker"
    assert "api_key_ref: PROVIDER_API_KEY" in out


def test_add_new_provider_block() -> None:
    out = set_deep_values(
        _BASE,
        {
            "models.providers.groq.base_url": "https://api.groq.com/openai/v1",
            "models.providers.groq.model": "llama-3.3-70b",
            "models.providers.groq.api_key_ref": "GROQ_API_KEY",
        },
    )
    data = yaml.safe_load(out)
    groq = data["models"]["providers"]["groq"]
    assert groq["base_url"] == "https://api.groq.com/openai/v1"
    assert groq["model"] == "llama-3.3-70b"
    assert groq["api_key_ref"] == "GROQ_API_KEY"
    # Существующий провайдер не тронут.
    assert data["models"]["providers"]["local"]["model"] == "deepseek/deepseek-chat"


def test_add_key_into_existing_nested_map() -> None:
    out = set_deep_value(_BASE, "models.providers.local.input_usd_per_mtok", 0.5)
    data = yaml.safe_load(out)
    assert data["models"]["providers"]["local"]["input_usd_per_mtok"] == 0.5


def test_create_whole_missing_section_chain() -> None:
    out = set_deep_value(_BASE, "mcp.servers.fetch.command", "uvx")
    data = yaml.safe_load(out)
    assert data["mcp"]["servers"]["fetch"]["command"] == "uvx"


def test_list_value_rendered_flow_style() -> None:
    out = set_deep_value(_BASE, "mcp.servers.fetch.args", ["mcp-server-fetch", "--x"])
    data = yaml.safe_load(out)
    assert data["mcp"]["servers"]["fetch"]["args"] == ["mcp-server-fetch", "--x"]


def test_remove_nested_block() -> None:
    with_two = set_deep_values(
        _BASE,
        {
            "models.providers.groq.base_url": "https://api.groq.com/openai/v1",
            "models.providers.groq.model": "llama-3.3-70b",
        },
    )
    out = remove_deep_key(with_two, "models.providers.groq")
    data = yaml.safe_load(out)
    assert "groq" not in data["models"]["providers"]
    assert data["models"]["providers"]["local"]["model"] == "deepseek/deepseek-chat"


def test_remove_missing_is_noop() -> None:
    assert remove_deep_key(_BASE, "mcp.servers.nope") == _BASE
