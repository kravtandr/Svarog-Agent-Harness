"""Тесты Skill Loader (§6.4, §7): парсинг SKILL.md, сканирование, карточки,
read_skill tool, логирование SkillLoad в loop."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.schema import AutonomyMode, PoliciesConfig, RuntimeConfig
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.policy.engine import PolicyEngine
from svarog_harness.runtime.loop import AgentLoop
from svarog_harness.skills import scan_skills, skill_cards
from svarog_harness.skills.loader import SkillMetadataError, parse_metadata
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import RunState, SkillLoad
from svarog_harness.tools.base import RiskLevel
from svarog_harness.tools.registry import ToolRegistry
from svarog_harness.tools.skill_tools import ReadSkillTool
from svarog_harness.trace.recorder import TraceRecorder

_VALID_SKILL = """\
---
name: report-writer
description: Собрать отчёт по результатам задачи.
version: 0.2.0
risk: low
allowed_tools:
  - read_file
  - write_file
tags: [reporting]
---

# When to use

Когда пользователь просит подготовить отчёт.

# Workflow

1. Собрать данные.
2. Записать report.md.
"""


def _make_skill(root: Path, name: str, content: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


# --- парсинг метаданных ---


def test_parse_valid_metadata() -> None:
    meta, body = parse_metadata("report-writer", _VALID_SKILL)
    assert meta.name == "report-writer"
    assert meta.version == "0.2.0"
    assert meta.risk is RiskLevel.LOW
    assert meta.allowed_tools == ("read_file", "write_file")
    assert meta.tags == ("reporting",)
    assert "When to use" in body
    assert "---" not in body


def test_parse_missing_required_field() -> None:
    content = "---\nname: x\ndescription: y\nrisk: low\n---\nтело"
    with pytest.raises(SkillMetadataError, match="version"):
        parse_metadata("x", content)


def test_parse_invalid_risk() -> None:
    content = "---\nname: x\ndescription: y\nversion: 1.0.0\nrisk: extreme\n---\nтело"
    with pytest.raises(SkillMetadataError, match="risk"):
        parse_metadata("x", content)


def test_parse_no_frontmatter() -> None:
    with pytest.raises(SkillMetadataError, match="frontmatter"):
        parse_metadata("x", "# Просто заголовок\nбез frontmatter")


# --- сканирование ---


def test_scan_finds_skills_and_reports_errors(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _make_skill(skills_root, "report-writer", _VALID_SKILL)
    _make_skill(skills_root, "broken", "---\nname: broken\n---\nнет полей")

    scan = scan_skills([skills_root])
    assert [s.name for s in scan.skills] == ["report-writer"]
    assert len(scan.errors) == 1
    assert scan.errors[0].path.name == "broken"


def test_scan_deduplicates_by_name(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _make_skill(root_a, "dup", _VALID_SKILL.replace("report-writer", "dup"))
    _make_skill(root_b, "dup2", _VALID_SKILL.replace("report-writer", "dup"))

    scan = scan_skills([root_a, root_b])
    assert len(scan.skills) == 1  # первый выигрывает
    assert any("дубликат" in e.reason for e in scan.errors)


def test_scan_missing_dir_is_empty(tmp_path: Path) -> None:
    scan = scan_skills([tmp_path / "nope"])
    assert scan.skills == []
    assert scan.errors == []


def test_skill_cards_format(tmp_path: Path) -> None:
    _make_skill(tmp_path, "report-writer", _VALID_SKILL)
    scan = scan_skills([tmp_path])
    cards = skill_cards(scan.skills)
    assert "report-writer" in cards
    assert "v0.2.0" in cards
    assert "read_skill" in cards


# --- read_skill tool ---


async def test_read_skill_returns_body_and_logs(tmp_path: Path) -> None:
    _make_skill(tmp_path, "report-writer", _VALID_SKILL)
    scan = scan_skills([tmp_path])
    loaded: list[tuple[str, str | None]] = []
    tool = ReadSkillTool(scan.skills, on_load=lambda n, v: loaded.append((n, v)))

    result = await tool.call({"name": "report-writer"})
    assert result.ok
    assert "When to use" in result.output
    assert loaded == [("report-writer", "0.2.0")]


async def test_read_skill_unknown(tmp_path: Path) -> None:
    _make_skill(tmp_path, "report-writer", _VALID_SKILL)
    scan = scan_skills([tmp_path])
    result = await ReadSkillTool(scan.skills).call({"name": "нет-такого"})
    assert not result.ok
    assert result.error is not None
    assert "report-writer" in result.error


# --- интеграция с loop ---


class ScriptedProvider(ModelProvider):
    def __init__(self, turns: list[CompletionResult]) -> None:
        self.turns = list(turns)
        self.seen_messages: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.seen_messages.append(list(messages))
        return self.turns.pop(0)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_loop_logs_skill_load_and_injects_cards(db: AsyncSession, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_skill(ws / "skills", "report-writer", _VALID_SKILL)
    scan = scan_skills([ws / "skills"])
    sink: list[tuple[str, str | None]] = []
    registry = ToolRegistry()
    registry.register(ReadSkillTool(scan.skills, on_load=lambda n, v: sink.append((n, v))))
    provider = ScriptedProvider(
        [
            CompletionResult(
                content="",
                tool_calls=(
                    ToolCallRequest(
                        id="c1",
                        name="read_skill",
                        arguments_json='{"name": "report-writer"}',
                    ),
                ),
                usage=Usage(10, 5),
            ),
            CompletionResult(content="готово по скиллу", usage=Usage(10, 5)),
        ]
    )
    loop = AgentLoop(
        provider,
        registry,
        TraceRecorder(db),
        RuntimeConfig(),
        PolicyEngine(autonomy=AutonomyMode.YOLO, policies=PoliciesConfig(), workspace=ws),
        ws,
        model_name="test-model",
        skill_cards=skill_cards(scan.skills),
        skill_load_sink=sink,
    )
    outcome = await loop.run("подготовь отчёт", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    # Карточка скилла попала в системный промпт.
    system_message = provider.seen_messages[0][0]
    assert system_message.role == "system"
    assert "report-writer" in system_message.content

    # SkillLoad записан.
    load = (await db.execute(select(SkillLoad))).scalar_one()
    assert load.skill_name == "report-writer"
    assert load.skill_version == "0.2.0"
    assert load.source == "full"
