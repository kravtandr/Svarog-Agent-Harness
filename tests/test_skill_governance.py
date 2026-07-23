"""Тесты skill governance / Flow B (#26): валидация, proposal-ветки, merge/reject."""

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.config.loader import load_config
from svarog_harness.config.schema import AutonomyMode
from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.llm.provider import (
    ChatMessage,
    CompletionResult,
    ModelProvider,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from svarog_harness.runtime import orchestrator
from svarog_harness.runtime.orchestrator import RunHooks, TaskRunner
from svarog_harness.skills.proposal import SkillProposalRequest, validate_proposal
from svarog_harness.skills.proposal_manager import SkillProposalManager
from svarog_harness.storage.db import create_engine, create_session_factory, init_db
from svarog_harness.storage.models import SkillProposalStatus
from svarog_harness.trace.recorder import TraceRecorder

_VALID_SKILL = (
    "---\n"
    "name: greeter\n"
    "description: Приветствует пользователя по имени.\n"
    "version: 0.1.0\n"
    "risk: low\n"
    "provenance: agent\n"
    "---\n"
    "# When to use\nКогда нужно поздороваться.\n"
)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    path = tmp_path / "db" / "svarog.sqlite3"
    init_db(path)
    engine = create_engine(path)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _init_skills_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    repo = GitRepo(path)
    await repo.init()
    await repo.ensure_identity()
    (path / ".gitkeep").write_text("", encoding="utf-8")
    await repo.add_all()
    await repo.commit("init skills")


async def _run_id(db: AsyncSession) -> str:
    run = await TraceRecorder(db).start_run(task="t", autonomy="yolo", model="test")
    return run.id


def _request(**over: object) -> SkillProposalRequest:
    base = {
        "skill_name": "greeter",
        "action": "create",
        "files": {"SKILL.md": _VALID_SKILL},
        "note": "переиспользуемое приветствие",
    }
    base.update(over)
    return SkillProposalRequest(**base)  # type: ignore[arg-type]


# --- валидация ---


def test_validate_ok() -> None:
    assert validate_proposal(_request()) == []


def test_validate_requires_provenance_agent() -> None:
    skill = _VALID_SKILL.replace("provenance: agent\n", "")
    errors = validate_proposal(_request(files={"SKILL.md": skill}))
    assert any("provenance: agent" in e for e in errors)


def test_validate_name_mismatch() -> None:
    errors = validate_proposal(_request(skill_name="other"))
    assert any("не совпадает" in e for e in errors)


def test_validate_missing_skill_md() -> None:
    errors = validate_proposal(_request(files={"scripts/x.sh": "echo hi"}))
    assert errors == ["proposal должен содержать SKILL.md"]


# --- 0.1 (ADR-0015): path-traversal в proposal → произвольная запись на хост --


def test_validate_rejects_traversal_in_file_key() -> None:
    errors = validate_proposal(
        _request(files={"SKILL.md": _VALID_SKILL, "../../../.ssh/authorized_keys": "ssh-rsa ..."})
    )
    assert any("выходит за пределы каталога" in e for e in errors)


def test_validate_rejects_absolute_file_key() -> None:
    errors = validate_proposal(
        _request(files={"SKILL.md": _VALID_SKILL, "/etc/cron.d/evil": "* * * * * root id"})
    )
    assert any("выходит за пределы каталога" in e for e in errors)


@pytest.mark.parametrize("bad_name", ["../..", "../evil", ".hidden", "a/b", "evil/../.."])
def test_validate_rejects_traversal_in_skill_name(bad_name: str) -> None:
    errors = validate_proposal(_request(skill_name=bad_name))
    assert any("недопустимое имя скилла" in e for e in errors)


async def test_persist_traversal_does_not_escape_repo(db: AsyncSession, tmp_path: Path) -> None:
    """Reproducer: ключ files с `..` не пишет за пределы skills-репозитория."""
    skills_dir = tmp_path / "skills"
    await _init_skills_repo(skills_dir)
    outside = tmp_path / "victim.txt"
    manager = SkillProposalManager(db, skills_dir)
    proposal = await manager.persist(
        _request(files={"SKILL.md": _VALID_SKILL, "../../victim.txt": "pwned"})
    )
    # Заявка отклонена валидацией, ветка не создана, файл вне репо не появился.
    assert proposal.status is SkillProposalStatus.FAILED
    assert not outside.exists()
    assert not (tmp_path / "victim.txt").exists()


# --- менеджер: persist / merge / reject ---


async def test_persist_creates_pending_branch(db: AsyncSession, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    await _init_skills_repo(skills_dir)
    manager = SkillProposalManager(db, skills_dir)
    proposal = await manager.persist(_request(source_run_id=await _run_id(db)))

    assert proposal.status is SkillProposalStatus.PENDING
    assert proposal.branch and proposal.branch.startswith("svarog/skill/greeter-")
    assert "greeter" in (proposal.diff or "")
    # На базовой ветке скилла ещё нет — только в proposal-ветке.
    assert not (skills_dir / "greeter" / "SKILL.md").exists()
    assert await GitRepo(skills_dir).branch_exists(proposal.branch)


async def test_approve_merges_into_base(db: AsyncSession, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    await _init_skills_repo(skills_dir)
    manager = SkillProposalManager(db, skills_dir)
    proposal = await manager.persist(_request(source_run_id=await _run_id(db)))

    sha = await manager.decide(proposal, approved=True, decided_by="test")
    assert sha
    assert proposal.status is SkillProposalStatus.MERGED
    # После merge скилл появился на базовой ветке.
    merged = skills_dir / "greeter" / "SKILL.md"
    assert merged.read_text(encoding="utf-8") == _VALID_SKILL
    assert not await GitRepo(skills_dir).branch_exists(proposal.branch or "")


async def test_reject_deletes_branch(db: AsyncSession, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    await _init_skills_repo(skills_dir)
    manager = SkillProposalManager(db, skills_dir)
    proposal = await manager.persist(_request(source_run_id=await _run_id(db)))
    branch = proposal.branch or ""

    await manager.decide(proposal, approved=False, decided_by="test", reason="дубликат")
    assert proposal.status is SkillProposalStatus.REJECTED
    assert not (skills_dir / "greeter" / "SKILL.md").exists()
    assert not await GitRepo(skills_dir).branch_exists(branch)


async def test_persist_invalid_records_failed(db: AsyncSession, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    await _init_skills_repo(skills_dir)
    manager = SkillProposalManager(db, skills_dir)
    skill = _VALID_SKILL.replace("provenance: agent\n", "")
    proposal = await manager.persist(_request(files={"SKILL.md": skill}))

    assert proposal.status is SkillProposalStatus.FAILED
    assert SkillProposalManager.validation_messages(proposal)
    assert proposal.branch is None


async def test_persist_refuses_dir_nested_in_foreign_repo(db: AsyncSession, tmp_path: Path) -> None:
    """Reproducer: skills-каталог ВНУТРИ чужого репозитория — не skills-репозиторий.

    Кампания 23.07.2026: `skills.paths` указывал на `agent-home/skills` внутри
    рабочего дерева самого Svarog. `is_repo()` отвечает «я внутри какого-то
    репозитория», поэтому proposal материализовался в ЧУЖОМ репо: `add_all`
    смёл в коммит всё незакоммиченное рабочее дерево, а `checkout` обратно на
    базовую ветку его выбросил. Граница — сравнение toplevel с самим путём.
    """
    outer = tmp_path / "outer"
    (outer / "skills").mkdir(parents=True)
    repo = GitRepo(outer)
    await repo.init()
    await repo.ensure_identity()
    (outer / "tracked.txt").write_text("исходное\n", encoding="utf-8")
    await repo.add_all()
    await repo.commit("init outer")
    # Незакоммиченная работа человека в чужом репозитории.
    (outer / "tracked.txt").write_text("несохранённая работа\n", encoding="utf-8")
    (outer / "new.txt").write_text("новый файл\n", encoding="utf-8")
    base = await repo.current_branch()

    proposal = await SkillProposalManager(db, outer / "skills").persist(_request())

    assert proposal.status is SkillProposalStatus.FAILED
    assert proposal.branch is None
    # Чужое рабочее дерево не тронуто: ни ветки, ни коммита, ни потери правок.
    assert await repo.current_branch() == base
    assert (outer / "tracked.txt").read_text(encoding="utf-8") == "несохранённая работа\n"
    assert (outer / "new.txt").exists()


# --- интеграция: run порождает proposal ---


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


def _patch_provider(monkeypatch: pytest.MonkeyPatch, turns: list[CompletionResult]) -> None:
    class ScriptedProvider(ModelProvider):
        def __init__(self) -> None:
            self.turns = list(turns)

        async def complete(
            self,
            messages: list[ChatMessage],
            tools: list[ToolDefinition],
            *,
            on_text_delta: Callable[[str], None] | None = None,
        ) -> CompletionResult:
            return self.turns.pop(0)

    provider = ScriptedProvider()
    monkeypatch.setattr(orchestrator, "default_provider", lambda models_cfg, store=None: provider)


async def test_run_creates_skill_proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    await _init_skills_repo(ws / "skills")

    args = json.dumps(
        {"name": "greeter", "skill_md": _VALID_SKILL, "note": "приветствие"},
        ensure_ascii=False,
    )
    _patch_provider(
        monkeypatch,
        [
            CompletionResult(
                content="",
                tool_calls=(
                    ToolCallRequest(id="c1", name="create_skill_proposal", arguments_json=args),
                ),
                usage=Usage(10, 5),
            ),
            CompletionResult(content="Предложил скилл", usage=Usage(10, 5)),
        ],
    )
    runner = TaskRunner(load_config(project_dir=ws), ws)
    outcome = await runner.run_once("предложи скилл", AutonomyMode.YOLO, hooks=RunHooks())

    async def fetch(db: AsyncSession) -> list[str]:
        pending = await SkillProposalManager(db, ws / "skills").list_pending()
        return [p.skill_name for p in pending]

    assert outcome.state.value == "completed"
    assert await runner.with_db(fetch) == ["greeter"]
