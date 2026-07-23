"""Команды `svarog skills`: список, проверка, proposals, курирование, pin (§6.4, §18).

Вынесено из main.py: общие хелперы берутся из cli/_shared.py, поэтому модуль
не импортирует main.py и цикла не создаёт.
"""

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy.ext.asyncio import AsyncSession

from svarog_harness.cli._shared import console, known_secret_values, load_config_or_exit
from svarog_harness.cli.chat_engine import with_db
from svarog_harness.config.paths import skills_dirs
from svarog_harness.config.schema import SvarogConfig
from svarog_harness.gitflow import GitError
from svarog_harness.llm.openai_compatible import ApiKeyError, auxiliary_provider
from svarog_harness.secrets import default_secret_store
from svarog_harness.skills import scan_skills
from svarog_harness.skills.curator import (
    CurationReport,
    CuratorStore,
    consolidate_layer2,
    prune_layer1,
    rewrite_description,
)
from svarog_harness.skills.models import Skill
from svarog_harness.skills.proposal import SkillProposalRequest
from svarog_harness.skills.proposal_manager import (
    SkillProposalManager,
    SkillProposalNotFoundError,
    SkillProposalStateError,
)
from svarog_harness.storage.models import SkillProposal

skills_app = typer.Typer(help="Скиллы: список и проверка.", no_args_is_help=True)


@skills_app.command("list")
def skills_list() -> None:
    """Показать доступные скиллы и их карточки."""
    cfg = load_config_or_exit()
    workspace = Path.cwd().resolve()
    scan = scan_skills(skills_dirs(cfg, workspace))
    if not scan.skills and not scan.errors:
        console.print("скиллов не найдено (проверьте skills.paths в svarog.yaml)")
        return
    for skill in scan.skills:
        console.print(skill.card())
    for skill_error in scan.errors:
        console.print(f"[yellow]пропущен ({skill_error.path.name}): {skill_error.reason}[/yellow]")


@skills_app.command("check")
def skills_check() -> None:
    """Проверить валидность SKILL.md всех скиллов; exit code 1 при ошибках."""
    cfg = load_config_or_exit()
    workspace = Path.cwd().resolve()
    scan = scan_skills(skills_dirs(cfg, workspace))
    for skill in scan.skills:
        console.print(f"[green]ok[/green] {skill.name} (v{skill.metadata.version})")
    for skill_error in scan.errors:
        console.print(f"[red]ошибка[/red] {skill_error.path}: {skill_error.reason}")
    if scan.errors:
        raise typer.Exit(code=1)
    console.print(f"проверено скиллов: {len(scan.skills)}, ошибок нет")


proposals_app = typer.Typer(
    help="Skill proposals (Flow B): review, merge, reject.", no_args_is_help=True
)
skills_app.add_typer(proposals_app, name="proposals")


def _proposals_skills_dir(cfg: SvarogConfig) -> Path:
    dirs = skills_dirs(cfg, Path.cwd().resolve())
    if not dirs:
        console.print("[red]skills.paths пуст в svarog.yaml[/red]")
        raise typer.Exit(code=1)
    return dirs[0]


@proposals_app.command("list")
def skills_proposals_list() -> None:
    """Показать skill proposals, ожидающие review (Flow B, §18)."""
    cfg = load_config_or_exit()
    skills_dir = _proposals_skills_dir(cfg)

    async def action(db: AsyncSession) -> None:
        proposals = await SkillProposalManager(db, skills_dir).list_pending()
        if not proposals:
            console.print("ожидающих skill proposals нет")
            return
        for proposal in proposals:
            console.print(
                f"[cyan]{proposal.id[:8]}[/cyan] {proposal.skill_name} "
                f"({proposal.action}) → ветка {proposal.branch}"
            )
            console.print(
                f"  [dim]review: svarog skills proposals show {proposal.id[:8]} → "
                f"approve/reject {proposal.id[:8]}[/dim]"
            )

    asyncio.run(with_db(cfg, action))


@proposals_app.command("show")
def skills_proposals_show(
    proposal_id: Annotated[str, typer.Argument(help="id proposal'а или его префикс")],
) -> None:
    """Показать diff и метаданные skill proposal (фактические изменения, §12)."""
    cfg = load_config_or_exit()
    skills_dir = _proposals_skills_dir(cfg)

    async def action(db: AsyncSession) -> None:
        try:
            proposal = await SkillProposalManager(db, skills_dir).get(proposal_id)
        except SkillProposalNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        console.print(f"[bold]proposal {proposal.id[:8]}[/bold] | {proposal.status.value}")
        console.print(f"  скилл: {proposal.skill_name} ({proposal.action})")
        console.print(f"  ветка: {proposal.branch} → {proposal.base}")
        if proposal.note:
            console.print(f"  примечание: {proposal.note}")
        for message in SkillProposalManager.validation_messages(proposal):
            console.print(f"  [yellow]валидация: {message}[/yellow]")
        console.print(proposal.diff or "(diff пуст)")

    asyncio.run(with_db(cfg, action))


def _decide_proposal(proposal_id: str, *, approved: bool, reason: str | None) -> None:
    cfg = load_config_or_exit()
    skills_dir = _proposals_skills_dir(cfg)

    async def action(db: AsyncSession) -> tuple[SkillProposal, str | None]:
        manager = SkillProposalManager(db, skills_dir)
        proposal = await manager.get(proposal_id)
        sha = await manager.decide(proposal, approved=approved, decided_by="cli", reason=reason)
        return proposal, sha

    try:
        proposal, sha = asyncio.run(with_db(cfg, action))
    except SkillProposalNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    except (SkillProposalStateError, GitError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    if approved:
        console.print(f"[green]proposal {proposal.id[:8]} влит[/green] в {proposal.base} ({sha})")
    else:
        console.print(f"[yellow]proposal {proposal.id[:8]} отклонён[/yellow]")


@proposals_app.command("approve")
def skills_proposals_approve(
    proposal_id: Annotated[str, typer.Argument(help="id proposal'а или его префикс")],
    reason: Annotated[str | None, typer.Option("--reason", help="Комментарий")] = None,
) -> None:
    """Одобрить и влить skill proposal в базовую ветку (§18)."""
    _decide_proposal(proposal_id, approved=True, reason=reason)


@proposals_app.command("reject")
def skills_proposals_reject(
    proposal_id: Annotated[str, typer.Argument(help="id proposal'а или его префикс")],
    reason: Annotated[str | None, typer.Option("--reason", help="Причина отказа")] = None,
) -> None:
    """Отклонить skill proposal и удалить его ветку."""
    _decide_proposal(proposal_id, approved=False, reason=reason)


@skills_app.command("curate")
def skills_curate(
    semantic: Annotated[
        bool,
        typer.Option("--semantic", help="Слой 2: LLM-консолидация на auxiliary-модели (opt-in)"),
    ] = False,
) -> None:
    """Curator: слой 1 (lifecycle по usage) и опц. слой 2 (LLM-консолидация, §18.1)."""
    cfg = load_config_or_exit()
    workspace = Path.cwd().resolve()
    scan = scan_skills(skills_dirs(cfg, workspace))

    async def action(db: AsyncSession) -> None:
        transitions = await prune_layer1(db, scan.skills, cfg.curator)
        if transitions:
            for t in transitions:
                console.print(
                    f"[cyan]{t.skill_name}[/cyan]: {t.old.value} → {t.new.value} "
                    f"[dim]({t.reason})[/dim]"
                )
        else:
            console.print("curator слой 1: lifecycle-изменений нет")
        if semantic or cfg.curator.semantic:
            await _curate_semantic(cfg, workspace, scan.skills, db)

    asyncio.run(with_db(cfg, action))


async def _curate_semantic(
    cfg: SvarogConfig, workspace: Path, skills: list[Skill], db: AsyncSession
) -> None:
    """Слой 2: LLM-находки → отчёт в artifacts/ + description-proposals (§18.1)."""
    try:
        provider = auxiliary_provider(cfg.models, default_secret_store(cfg.secrets.path))
    except ApiKeyError as exc:
        console.print(f"[red]слой 2 недоступен: {exc}[/red]")
        return
    console.print("[dim]curator слой 2: анализ библиотеки auxiliary-моделью…[/dim]")
    report = await consolidate_layer2(provider, skills)
    path = _write_curation_report(workspace, report)
    console.print(f"[dim]отчёт: {path}[/dim]")
    if report.parse_error:
        console.print(
            f"[yellow]слой 2: LLM вернул неразбираемый ответ ({report.parse_error})[/yellow]"
        )
        return
    if not report.findings:
        console.print("curator слой 2: находок нет")
        return
    for finding in report.findings:
        console.print(
            f"[magenta]{finding.kind}[/magenta] {', '.join(finding.skills)}: {finding.detail}"
        )
    await _propose_description_improvements(cfg, workspace, skills, report, db)


def _write_curation_report(workspace: Path, report: CurationReport) -> Path:
    from datetime import UTC, datetime

    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = artifacts / f"skill-curation-{stamp}.md"
    path.write_text(report.to_markdown(), encoding="utf-8")
    return path


async def _propose_description_improvements(
    cfg: SvarogConfig,
    workspace: Path,
    skills: list[Skill],
    report: CurationReport,
    db: AsyncSession,
) -> None:
    """Содержательные правки — только через proposals (Flow B, §18.1)."""
    by_name = {s.name: s for s in skills}
    skills_dir = skills_dirs(cfg, workspace)[0] if cfg.skills.paths else None
    if skills_dir is None:
        return
    manager = SkillProposalManager(db, skills_dir)
    store = default_secret_store(cfg.secrets.path)
    for finding in report.improvements():
        for name in finding.skills:
            skill = by_name.get(name)
            # Curator предлагает правки только agent-created скиллов (§18.1).
            if skill is None or skill.metadata.provenance != "agent":
                continue
            files = {"SKILL.md": rewrite_description(skill, finding.suggested_description or "")}
            request = SkillProposalRequest(
                skill_name=name,
                action="update",
                files=files,
                note=f"curator: улучшить описание — {finding.detail}",
            )
            proposal = await manager.persist(request, known_values=known_secret_values(cfg, store))
            console.print(
                f"[cyan]proposal[/cyan] {name}: обновить описание "
                f"[dim]({proposal.status.value}, {proposal.id[:8]})[/dim]"
            )


def _set_pin(name: str, pinned: bool) -> None:
    cfg = load_config_or_exit()

    async def action(db: AsyncSession) -> None:
        await CuratorStore(db).set_pinned(name, pinned)

    asyncio.run(with_db(cfg, action))
    verb = "закреплён" if pinned else "откреплён"
    console.print(f"скилл '{name}' {verb} (pinned={str(pinned).lower()})")


@skills_app.command("pin")
def skills_pin(
    name: Annotated[str, typer.Argument(help="Имя скилла")],
) -> None:
    """Закрепить скилл: вывести из-под автоматических lifecycle-переходов (§18.1)."""
    _set_pin(name, True)


@skills_app.command("unpin")
def skills_unpin(
    name: Annotated[str, typer.Argument(help="Имя скилла")],
) -> None:
    """Снять закрепление скилла."""
    _set_pin(name, False)
