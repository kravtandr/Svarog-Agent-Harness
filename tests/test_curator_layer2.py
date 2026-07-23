"""Тесты Skill Curator слой 2 (#28): LLM-консолидация, отчёт, description-proposals."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from svarog_harness.cli import main as cli_main
from svarog_harness.cli import skills_commands
from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolDefinition,
    Usage,
)
from svarog_harness.skills.curator import consolidate_layer2, rewrite_description
from svarog_harness.skills.loader import parse_metadata, scan_skills

runner = CliRunner()


class ScriptedProvider(ModelProvider):
    def __init__(self, content: str) -> None:
        self._content = content
        self.seen: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        self.seen.append(messages)
        return CompletionResult(content=self._content, usage=Usage(10, 5))


def _skill_md(name: str, provenance: str = "agent", description: str = "Описание.") -> str:
    return (
        f"---\nname: {name}\ndescription: {description}\nversion: 0.1.0\n"
        f"risk: low\nprovenance: {provenance}\n---\n# When to use\nиногда.\n"
    )


def _make_skills(root: Path, names: dict[str, str]) -> None:
    for name, provenance in names.items():
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(_skill_md(name, provenance), encoding="utf-8")


async def test_consolidate_parses_findings(tmp_path: Path) -> None:
    _make_skills(tmp_path / "skills", {"a": "agent", "b": "agent"})
    skills = scan_skills([tmp_path / "skills"]).skills
    payload = json.dumps(
        {
            "findings": [
                {"kind": "duplicate", "skills": ["a", "b"], "detail": "почти одно и то же"},
                {
                    "kind": "improve_description",
                    "skills": ["a"],
                    "detail": "размыто",
                    "suggested_description": "Чёткое новое описание",
                },
                {"kind": "bogus", "skills": ["a"], "detail": "игнор"},
            ]
        }
    )
    report = await consolidate_layer2(ScriptedProvider(payload), skills)
    assert [f.kind for f in report.findings] == ["duplicate", "improve_description"]
    assert report.improvements()[0].suggested_description == "Чёткое новое описание"


async def test_consolidate_handles_code_fences(tmp_path: Path) -> None:
    _make_skills(tmp_path / "skills", {"a": "agent"})
    skills = scan_skills([tmp_path / "skills"]).skills
    fenced = '```json\n{"findings": []}\n```'
    report = await consolidate_layer2(ScriptedProvider(fenced), skills)
    assert report.parse_error is None
    assert report.findings == []


async def test_consolidate_parse_error(tmp_path: Path) -> None:
    _make_skills(tmp_path / "skills", {"a": "agent"})
    skills = scan_skills([tmp_path / "skills"]).skills
    report = await consolidate_layer2(ScriptedProvider("это не json"), skills)
    assert report.parse_error is not None
    assert "неразбираемый" in report.to_markdown()


async def test_consolidate_empty_library_no_call() -> None:
    provider = ScriptedProvider("{}")
    report = await consolidate_layer2(provider, [])
    assert report.findings == []
    assert provider.seen == []  # пустая библиотека — LLM не вызывается


def test_rewrite_description_preserves_frontmatter(tmp_path: Path) -> None:
    _make_skills(tmp_path / "skills", {"a": "agent"})
    skill = scan_skills([tmp_path / "skills"]).skills[0]
    content = rewrite_description(skill, "Совсем новое описание")
    metadata, _ = parse_metadata("a", content)
    assert metadata.description == "Совсем новое описание"
    assert metadata.provenance == "agent"  # provenance сохранён
    assert metadata.version == "0.1.0"


def test_curate_semantic_creates_description_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "svarog.yaml").write_text(
        "models:\n  default: local\n  providers:\n    local:\n"
        "      base_url: http://localhost:9/v1\n      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {tmp_path / 'state' / 'svarog.db'}\n",
        encoding="utf-8",
    )
    _make_skills(ws / "skills", {"greeter": "agent"})

    async def init_skills_repo() -> None:
        repo = GitRepo(ws / "skills")
        await repo.init()
        await repo.ensure_identity()
        await repo.add_all()
        await repo.commit("init skills")

    asyncio.run(init_skills_repo())
    monkeypatch.chdir(ws)
    monkeypatch.setenv("HOME", str(tmp_path))

    payload = json.dumps(
        {
            "findings": [
                {
                    "kind": "improve_description",
                    "skills": ["greeter"],
                    "detail": "слишком общо",
                    "suggested_description": "Приветствует пользователя по имени.",
                }
            ]
        }
    )
    # Патчим модуль, который владеет символом: команды skills живут в
    # cli/skills_commands.py, main.py их только регистрирует.
    monkeypatch.setattr(
        skills_commands,
        "auxiliary_provider",
        lambda models_cfg, store=None: ScriptedProvider(payload),
    )

    result = runner.invoke(cli_main.app, ["skills", "curate", "--semantic"])
    assert result.exit_code == 0, result.output
    assert "improve_description" in result.output
    assert "обновить описание" in result.output
    # Отчёт записан в artifacts/.
    assert list((ws / "artifacts").glob("skill-curation-*.md"))
