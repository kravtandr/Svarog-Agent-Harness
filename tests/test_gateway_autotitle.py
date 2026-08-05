"""Автоназвание чатов по содержанию (спека 2026-08-04, 2026-08-05): хук GatewayService."""

import asyncio
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
    """Aux-модель названий: скриптованные ответы по фазам, пишет промпты."""

    def __init__(self, titles: list[str], *, error: bool = False) -> None:
        self.titles = list(titles)
        self.error = error
        self.prompts: list[str] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.prompts.append(messages[-1].content)
        if self.error:
            raise RuntimeError("aux недоступна")
        return CompletionResult(content=self.titles.pop(0), usage=Usage(1, 1))


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


def _spy_events(service: GatewayService) -> list[dict[str, Any]]:
    """Собрать публикуемые события: publish-шпион поверх настоящего hub'а."""
    events: list[dict[str, Any]] = []
    original = service.session_events.publish

    def spy(event: dict[str, Any]) -> None:
        events.append(event)
        original(event)

    service.session_events.publish = spy  # type: ignore[method-assign]
    return events


async def test_draft_then_refine_with_events(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("Париж — столица Франции")])
    aux = TitleProvider(["Черновик названия", "Финальное название"])
    _patch_title(monkeypatch, aux)
    events = _spy_events(service)

    view = await service.create_session(title="Новый чат")
    await service.send_message(view.session_id, "Какая столица Франции?", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "Финальное название"
    assert meta["autotitle"] == "done"
    assert meta["autotitle_draft"] == "Черновик названия"
    # Черновик — по одному вопросу, уточнение — с ответом.
    assert "Ответ:" not in aux.prompts[0]
    assert "Ответ:" in aux.prompts[1]
    assert [e["phase"] for e in events] == ["draft", "final"]
    assert events[0]["title"] == "Черновик названия"
    assert events[1]["title"] == "Финальное название"
    assert all(e["type"] == "session_title" and e["session_id"] == view.session_id for e in events)


async def test_refine_equal_to_draft_publishes_once(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("ответ")])
    aux = TitleProvider(["Одно название", "Одно название"])
    _patch_title(monkeypatch, aux)
    events = _spy_events(service)

    view = await service.create_session(title="Новый чат")
    await service.send_message(view.session_id, "вопрос", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "Одно название"
    assert meta["autotitle"] == "done"
    assert [e["phase"] for e in events] == ["draft"]


async def test_aux_error_falls_back_to_truncated_question(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("ответ")])
    aux = TitleProvider([], error=True)
    _patch_title(monkeypatch, aux)
    events = _spy_events(service)

    view = await service.create_session(title="Новый чат")
    await service.send_message(view.session_id, "Какая столица Франции?", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    # Черновик = fallback-обрезка; уточнение тоже упало -> черновик остаётся.
    assert title == "Какая столица Франции?"
    assert meta["autotitle"] == "done"
    assert [e["phase"] for e in events] == ["draft"]


class _RaceProvider(ModelProvider):
    """Форсирует гонку черновик/уточнение (ревью задачи 3, 2026-08-05).

    Вызов черновика (первый) держится на event'е, пока не отпущен: read
    уточнения успевает пройти по сессии БЕЗ черновика. Вызов уточнения
    (второй) сначала отпускает черновик и дожидается, что его фоновая
    задача реально закоммитила запись (await самой задачи из _tasks), и
    только потом падает — так «read уточнения раньше write черновика, а
    aux уточнения падает» гарантированно, а не по случайному тайминиг
    event loop'а.
    """

    def __init__(self, service: GatewayService, draft_title: str) -> None:
        self._service = service
        self._draft_title = draft_title
        self.calls = 0
        self._draft_release = asyncio.Event()

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.calls += 1
        if self.calls == 1:  # фаза черновика — держим её, пока не разрешим
            await self._draft_release.wait()
            return CompletionResult(content=self._draft_title, usage=Usage(1, 1))
        # Фаза уточнения: отпускаем черновик и ждём, что он реально
        # закоммитился, прежде чем упасть.
        self._draft_release.set()
        current = asyncio.current_task()
        others = [t for t in self._service._tasks if t is not current and not t.done()]
        if others:
            await asyncio.gather(*others, return_exceptions=True)
        raise RuntimeError("aux недоступна на уточнении")


async def test_refine_aux_failure_keeps_generated_draft(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Черновик успевает приехать в БД между read и write уточнения.

    Сбой aux-модели на уточнении не должен перетереть только что
    закоммиченный черновик fallback-обрезкой — стухший снимок had_draft из
    read() был источником этого бага (ревью задачи 3, 2026-08-05).
    """
    _patch_agent(monkeypatch, [_final("ответ")])
    aux = _RaceProvider(service, "Хороший черновик")
    _patch_title(monkeypatch, aux)
    events = _spy_events(service)

    view = await service.create_session(title="Новый чат")
    await service.send_message(view.session_id, "вопрос", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    # Черновик уже сгенерирован моделью -> сбой уточнения его не портит.
    assert title == "Хороший черновик"
    assert meta["autotitle"] == "done"
    assert [e["phase"] for e in events] == ["draft"]


async def test_manually_renamed_draft_not_overwritten(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("ответ")])
    aux = TitleProvider(["Не должно применяться"])
    _patch_title(monkeypatch, aux)

    view = await service.create_session(title="Новый чат")

    async def rename(db: Any) -> None:
        row = await db.get(Session, view.session_id)
        row.title = "Ручное имя"
        row.meta = {**(row.meta or {}), "autotitle": "draft", "autotitle_draft": "Черновик"}
        await db.commit()

    await service._read(rename)
    events = _spy_events(service)
    await service.send_message(view.session_id, "вопрос", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "Ручное имя"
    assert meta["autotitle"] == "draft"  # уточнение не сработало и флаг не тронут
    assert events == []
    assert aux.prompts == []  # ни одна фаза не звала модель


async def test_custom_title_untouched(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_agent(monkeypatch, [_final("ответ")])
    aux = TitleProvider(["Не должно применяться"])
    _patch_title(monkeypatch, aux)
    events = _spy_events(service)

    view = await service.create_session(title="Мой проект")
    await service.send_message(view.session_id, "вопрос", None)
    await service.wait_for_background()

    title, meta = await _session_state(service, view.session_id)
    assert title == "Мой проект"
    assert "autotitle" not in meta
    assert events == []
    assert aux.prompts == []


def test_session_events_ws_auth(service: GatewayService) -> None:
    from fastapi.testclient import TestClient

    from svarog_harness.gateway.api import create_app

    client = TestClient(create_app(service, bearer_token="secret-token"))
    try:
        with client.websocket_connect("/sessions/events"):
            pass
        raise AssertionError("без токена соединение должно быть отклонено")
    except AssertionError:
        raise
    except Exception:
        pass  # 1008 policy violation — ожидаемо
    with client.websocket_connect("/sessions/events?token=secret-token") as ws:
        # Успешный handshake подтверждает auth-путь; доставка событий покрыта
        # publish-шпионами выше (TestClient обрывает fire-and-forget задачи).
        ws.close()


def test_workspace_hub_shares_session_events(tmp_path: Path) -> None:
    from svarog_harness.gateway.hub import WorkspaceHub
    from svarog_harness.gateway.roots import WorkspaceRootsRegistry

    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    _write_config(root_a, tmp_path)
    hub = WorkspaceHub(
        base_cfg=load_config(project_dir=root_a),
        default_root=root_a,
        registry=WorkspaceRootsRegistry(tmp_path / "roots.json"),
    )
    svc_b = hub.service_for(root_b)
    assert svc_b.session_events is hub.service_for(root_a).session_events
