"""Автоназвание чатов по содержанию (спека 2026-08-04): хук GatewayService."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import ModelsConfig
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway import service as service_module
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolDefinition,
    Usage,
)
from svarog_harness.runtime import run_assembly
from svarog_harness.secrets import SecretStore
from svarog_harness.storage.models import Session


class ScriptedProvider(ModelProvider):
    """Основной агент: отдаёт заранее заданные ходы."""

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


class TitleProvider(ModelProvider):
    """Aux-модель названий: один и тот же ответ, считает вызовы."""

    def __init__(self, content: str = "Название чата", *, error: bool = False) -> None:
        self.content = content
        self.error = error
        self.calls = 0

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.calls += 1
        if self.error:
            raise RuntimeError("aux недоступна")
        return CompletionResult(content=self.content, usage=Usage(1, 1))


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
    cfg = load_config(project_dir=ws)
    return GatewayService(cfg, ws)


def _patch_agent(monkeypatch: pytest.MonkeyPatch, turns: list[CompletionResult]) -> None:
    provider = ScriptedProvider(turns)

    def fake_default_provider(
        models_cfg: ModelsConfig, store: object = None, workspace: object = None
    ) -> ModelProvider:
        return provider

    monkeypatch.setattr(run_assembly, "default_provider", fake_default_provider)


def _patch_title(monkeypatch: pytest.MonkeyPatch, provider: ModelProvider) -> None:
    def fake_auxiliary(models_cfg: ModelsConfig, store: SecretStore | None = None) -> ModelProvider:
        return provider

    monkeypatch.setattr(service_module, "auxiliary_provider", fake_auxiliary)


def _final(content: str) -> CompletionResult:
    return CompletionResult(content=content, usage=Usage(10, 5), finish_reason="stop")


async def _session_state(
    service: GatewayService, session_id: str
) -> tuple[str | None, dict[str, Any]]:
    async def action(db: Any) -> tuple[str | None, dict[str, Any]]:
        row = await db.get(Session, session_id)
        return row.title, dict(row.meta or {})

    return await service._read(action)


async def test_autotitle_after_first_run(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("Париж — столица Франции")])
    aux = TitleProvider("«География Франции.»")
    _patch_title(monkeypatch, aux)

    view = await service.create_session(title="Новый чат")
    await service.send_message(view.session_id, "Какая столица Франции?", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "География Франции"
    assert meta["autotitle"] == "done"
    assert aux.calls == 1


async def test_autotitle_fallback_when_aux_fails(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("ответ")])
    aux = TitleProvider(error=True)
    _patch_title(monkeypatch, aux)

    view = await service.create_session(title="Новый чат")
    await service.send_message(view.session_id, "Какая столица Франции?", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "Какая столица Франции?"  # короткий вопрос -> без обрезки
    assert meta["autotitle"] == "fallback"


async def test_autotitle_generated_once(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("первый ответ"), _final("второй ответ")])
    aux = TitleProvider("Название чата")
    _patch_title(monkeypatch, aux)

    view = await service.create_session(title="Новый чат")
    await service.send_message(view.session_id, "первый вопрос", None)
    await service.wait_for_background()
    await service.send_message(view.session_id, "второй вопрос", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "Название чата"
    assert meta["autotitle"] == "done"
    assert aux.calls == 1


async def test_autotitle_keeps_custom_title(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("ответ")])
    aux = TitleProvider("Не должно применяться")
    _patch_title(monkeypatch, aux)

    view = await service.create_session(title="Мой проект")
    await service.send_message(view.session_id, "вопрос", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "Мой проект"
    assert "autotitle" not in meta
    assert aux.calls == 0
