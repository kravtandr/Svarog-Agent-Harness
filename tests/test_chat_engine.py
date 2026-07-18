"""ChatEngine (ADR-0018 фаза 1): реальный рантайм со скриптованным провайдером."""

from collections.abc import Callable
from pathlib import Path

import pytest

from svarog_harness.cli import chat_engine as chat_engine_module
from svarog_harness.cli.chat_engine import ChatEngine
from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import AutonomyMode, SvarogConfig
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolDefinition,
    Usage,
)
from svarog_harness.runtime import orchestrator
from svarog_harness.runtime.orchestrator import RunHooks
from svarog_harness.storage.models import RunState


class ScriptedProvider(ModelProvider):
    """Возвращает заготовленные ответы, запоминая переданные messages."""

    def __init__(self) -> None:
        self.turns: list[CompletionResult] = []
        self.seen: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.seen.append(list(messages))
        result = self.turns.pop(0)
        if on_text_delta is not None and result.content:
            on_text_delta(result.content)
        return result


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> ScriptedProvider:
    scripted = ScriptedProvider()

    def fake_default_provider(models_cfg: object, store: object = None) -> ModelProvider:
        return scripted

    monkeypatch.setattr(orchestrator, "default_provider", fake_default_provider)
    return scripted


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SvarogConfig:
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    return load_config(project_dir=ws)


def _workspace(cfg: SvarogConfig, tmp_path: Path) -> Path:
    return tmp_path / "ws"


def _answer(text: str) -> CompletionResult:
    return CompletionResult(content=text, usage=Usage(10, 5))


async def test_send_captures_session_and_carries_history(
    cfg: SvarogConfig, tmp_path: Path, provider: ScriptedProvider
) -> None:
    provider.turns = [_answer("Привет, я Свар"), _answer("Тебя зовут Аня")]
    async with ChatEngine(cfg, _workspace(cfg, tmp_path), AutonomyMode.YOLO, RunHooks()) as engine:
        start = await engine.start()
        assert start.session_id is None and start.history == [] and start.label is None

        first = await engine.send("привет, меня зовут Аня")
        assert first.state is RunState.COMPLETED
        session_id = engine.session_id
        assert session_id is not None

        second = await engine.send("как меня зовут?")
        assert second.state is RunState.COMPLETED
        assert engine.session_id == session_id  # та же session на весь диалог
        # История первого хода дошла до модели вторым прогоном.
        flattened = " ".join(m.content for m in provider.seen[1])
        assert "меня зовут Аня" in flattened and "Привет, я Свар" in flattened


async def test_history_is_capped(
    cfg: SvarogConfig,
    tmp_path: Path,
    provider: ScriptedProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat_engine_module, "CHAT_HISTORY_LIMIT", 4)
    provider.turns = [_answer(f"ответ {i}") for i in range(3)]
    async with ChatEngine(cfg, _workspace(cfg, tmp_path), AutonomyMode.YOLO, RunHooks()) as engine:
        await engine.start()
        for i in range(3):
            await engine.send(f"вопрос {i}")
        # 3 хода × 2 сообщения, кап 4 — остались два последних хода.
        assert [m.content for m in engine._history] == [
            "вопрос 1",
            "ответ 1",
            "вопрос 2",
            "ответ 2",
        ]


async def test_continue_and_fork_load_history(
    cfg: SvarogConfig, tmp_path: Path, provider: ScriptedProvider
) -> None:
    ws = _workspace(cfg, tmp_path)
    provider.turns = [_answer("сорок два")]
    async with ChatEngine(cfg, ws, AutonomyMode.YOLO, RunHooks()) as engine:
        await engine.start()
        await engine.send("сколько будет 6*7?")
        session_id = engine.session_id
        assert session_id is not None

    async with ChatEngine(cfg, ws, AutonomyMode.YOLO, RunHooks()) as engine:
        start = await engine.start(continue_ref=session_id[:8])
        assert start.session_id == session_id
        assert [m.content for m in start.history] == ["сколько будет 6*7?", "сорок два"]
        assert start.label is not None and "продолжаю сессию" in start.label

    async with ChatEngine(cfg, ws, AutonomyMode.YOLO, RunHooks()) as engine:
        start = await engine.start(fork_ref=session_id[:8])
        assert start.session_id is None  # форк — новая сессия с копией истории
        assert len(start.history) == 2
        assert start.label is not None and "форк" in start.label


async def test_switch_and_reset_session(
    cfg: SvarogConfig, tmp_path: Path, provider: ScriptedProvider
) -> None:
    ws = _workspace(cfg, tmp_path)
    provider.turns = [_answer("ок")]
    async with ChatEngine(cfg, ws, AutonomyMode.YOLO, RunHooks()) as engine:
        await engine.start()
        await engine.send("задача")
        session_id = engine.session_id
        assert session_id is not None

        engine.reset_session()
        assert engine.session_id is None

        start = await engine.switch_session(session_id[:8], fork=False)
        assert start.session_id == session_id and engine.session_id == session_id

        sessions = await engine.list_sessions()
        assert [s.session.id for s in sessions] == [session_id]
        preview = await engine.session_preview(session_id)
        assert preview[0]["content"] == "задача"


async def test_close_is_idempotent_and_start_required(
    cfg: SvarogConfig, tmp_path: Path, provider: ScriptedProvider
) -> None:
    engine = ChatEngine(cfg, _workspace(cfg, tmp_path), AutonomyMode.YOLO, RunHooks())
    with pytest.raises(AssertionError):
        await engine.send("до start")
    await engine.start()
    await engine.close()
    await engine.close()  # повторное закрытие — no-op
