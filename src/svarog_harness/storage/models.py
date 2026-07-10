"""SQLAlchemy-модели операционного состояния и аудита (§15 TASK.md, ADR-0007).

Все timestamps — наивные datetime в UTC (SQLite не хранит timezone; хелпер —
`utcnow`). ToolResult хранится внутри ToolCall (`result`), ApprovalDecision —
внутри Approval (`status`/`decided_*`): сущность и её исход всегда читаются
вместе. FileChange и GitCommit живут в Git-репозиториях (ADR-0003), в БД —
только ссылки внутри JSON-полей.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Наивный UTC-timestamp — единый формат времени в БД."""
    return datetime.now(UTC).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


class RunState(StrEnum):
    """Состояния run'а (§11, ADR-0005)."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolCallStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"  # заблокирован Policy Engine


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class MemoryChangeStatus(StrEnum):
    """Статус заявки в очереди single writer'а памяти (ADR-0004)."""

    PENDING = "pending"
    APPLIED = "applied"
    FAILED = "failed"


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"  # проверка не смогла выполниться
    SKIPPED = "skipped"


class SkillProposalStatus(StrEnum):
    """Статус skill proposal в governance-flow (Flow B, §18)."""

    PENDING = "pending"  # ветка создана, ждёт review
    MERGED = "merged"  # одобрен и влит в базовую ветку
    REJECTED = "rejected"  # отклонён, ветка удалена
    FAILED = "failed"  # не удалось создать (валидация/secret scan/не git-репо)


class SkillLifecycleStatus(StrEnum):
    """Lifecycle-статус скилла, которым управляет Curator слой 1 (§18.1, ADR-0009).

    Полный жизненный цикл (§18) шире (draft/deprecated/blocked); механический
    слой 1 оперирует только обратимой тройкой active↔stale↔archived.
    """

    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"


def _enum(enum_cls: type[StrEnum]) -> Enum:
    """Хранить StrEnum по значениям, строкой (переносимо между SQLite/Postgres)."""
    return Enum(
        enum_cls,
        native_enum=False,
        length=32,
        values_callable=lambda e: [member.value for member in e],
    )


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012
        dict[str, Any]: JSON,
        datetime: DateTime(),
    }


class TimestampedBase(Base):
    __abstract__ = True

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Session(TimestampedBase):
    """Диалог/контекст пользователя; содержит серию runs (например, `svarog chat`)."""

    __tablename__ = "sessions"

    title: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)

    # passive_deletes: каскад выполняет SQLite (ondelete=CASCADE), ORM не обнуляет FK.
    runs: Mapped[list["Run"]] = relationship(
        back_populates="session", cascade="all, delete", passive_deletes=True
    )


class Run(TimestampedBase):
    """AgentRun — возобновляемый state machine (§11)."""

    __tablename__ = "runs"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    state: Mapped[RunState] = mapped_column(_enum(RunState), default=RunState.PENDING, index=True)
    task: Mapped[str] = mapped_column(Text)
    # Режим автономии фиксируется при старте run и не перечитывается (ADR-0010).
    autonomy: Mapped[str] = mapped_column(String(32))
    iterations: Mapped[int] = mapped_column(default=0)
    tokens_used: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    # Per-workspace lease (ADR-0015 §0.5): рабочее дерево run'а + heartbeat.
    # Живой run бьётся heartbeat'ом; gateway отказывает во втором run'е на том
    # же workspace, а recovery приостанавливает только протухшие RUNNING.
    workspace: Mapped[str | None] = mapped_column(String(1024), index=True)
    heartbeat_at: Mapped[datetime | None]
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)

    session: Mapped[Session] = relationship(back_populates="runs")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="run",
        order_by="Message.index_in_run",
        cascade="all, delete",
        passive_deletes=True,
    )
    tool_calls: Mapped[list["ToolCall"]] = relationship(
        back_populates="run", cascade="all, delete", passive_deletes=True
    )
    checkpoints: Mapped[list["Checkpoint"]] = relationship(
        back_populates="run", cascade="all, delete", passive_deletes=True
    )


class Message(TimestampedBase):
    """Сообщение в контексте run'а: user/assistant/tool/system."""

    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("run_id", "index_in_run"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    index_in_run: Mapped[int]
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[dict[str, Any]] = mapped_column(default=dict)

    run: Mapped[Run] = relationship(back_populates="messages")


class ToolCall(TimestampedBase):
    """Вызов инструмента; результат (ToolResult §15) — в поле `result`."""

    __tablename__ = "tool_calls"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    tool_name: Mapped[str] = mapped_column(String(128), index=True)
    arguments: Mapped[dict[str, Any]] = mapped_column(default=dict)
    risk_level: Mapped[str | None] = mapped_column(String(32))
    # Решение Policy Engine: allow | notify | deny | require_approval (ADR-0010).
    policy_decision: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[ToolCallStatus] = mapped_column(_enum(ToolCallStatus))
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]

    run: Mapped[Run] = relationship(back_populates="tool_calls")


class Approval(TimestampedBase):
    """ApprovalRequest + ApprovalDecision (§15): запрос и решение человека."""

    __tablename__ = "approvals"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    tool_call_id: Mapped[str | None] = mapped_column(
        ForeignKey("tool_calls.id", ondelete="SET NULL")
    )
    # Типизированная операция critical-набора или профиля: git.push, secrets.reveal, …
    action_type: Mapped[str] = mapped_column(String(128))
    # Фактическая команда/diff, показываемые человеку (ADR-0010: approval видит факт).
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[ApprovalStatus] = mapped_column(
        _enum(ApprovalStatus), default=ApprovalStatus.PENDING, index=True
    )
    decided_at: Mapped[datetime | None]
    decided_by: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)


class Checkpoint(TimestampedBase):
    """Сериализованное состояние run'а для resume (ADR-0005, write-ahead)."""

    __tablename__ = "checkpoints"
    __table_args__ = (Index("ix_checkpoints_run_iteration", "run_id", "iteration"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    iteration: Mapped[int]
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)

    run: Mapped[Run] = relationship(back_populates="checkpoints")


class MemoryChange(TimestampedBase):
    """Очередь MemoryChangeRequest для single writer'а памяти (ADR-0004)."""

    __tablename__ = "memory_queue"

    status: Mapped[MemoryChangeStatus] = mapped_column(
        _enum(MemoryChangeStatus), default=MemoryChangeStatus.PENDING, index=True
    )
    change: Mapped[dict[str, Any]] = mapped_column(default=dict)
    source_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    applied_at: Mapped[datetime | None]
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)


class SkillLoad(TimestampedBase):
    """Факт загрузки скилла в контекст — сырьё для Skill Curator (ADR-0009)."""

    __tablename__ = "skill_loads"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    skill_name: Mapped[str] = mapped_column(String(128), index=True)
    skill_version: Mapped[str | None] = mapped_column(String(64))
    # card — попал в контекст карточкой; full — загружен целиком через read_skill.
    source: Mapped[str] = mapped_column(String(32), default="full")


class CheckResult(TimestampedBase):
    """Результат детерминированной проверки verifier'а (§6.11)."""

    __tablename__ = "check_results"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    check_name: Mapped[str] = mapped_column(String(128))
    status: Mapped[CheckStatus] = mapped_column(_enum(CheckStatus))
    output: Mapped[str | None] = mapped_column(Text)


class SkillProposal(TimestampedBase):
    """Skill proposal (Flow B, §18): изменение скилла через ветку + review.

    Содержимое proposal живёт в Git-ветке skills-репозитория (ADR-0003); в БД —
    метаданные governance-flow: статус, ветка, diff, результаты валидации.
    """

    __tablename__ = "skill_proposals"

    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    skill_name: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(32))  # create | update
    status: Mapped[SkillProposalStatus] = mapped_column(
        _enum(SkillProposalStatus), default=SkillProposalStatus.PENDING, index=True
    )
    branch: Mapped[str | None] = mapped_column(String(255))
    base: Mapped[str | None] = mapped_column(String(255))  # ветка, в которую мержится
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    diff: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    # Результаты валидации SKILL.md (список сообщений) — сырьё для review.
    checks: Mapped[dict[str, Any]] = mapped_column(default=dict)
    decided_at: Mapped[datetime | None]
    decided_by: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)


class SkillState(TimestampedBase):
    """Кураторское состояние скилла (§18.1, ADR-0009): lifecycle + pin.

    Usage-телеметрия остаётся в `skill_loads` (не во frontmatter); здесь —
    производный lifecycle-статус, который слой 1 применяет автоматически.
    `created_at` служит якорем «new skill»: свежий скилл не архивируется до
    первого использования. Только agent-created скиллы попадают под curator.
    """

    __tablename__ = "skill_states"

    skill_name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    provenance: Mapped[str] = mapped_column(String(32), default="agent")
    status: Mapped[SkillLifecycleStatus] = mapped_column(
        _enum(SkillLifecycleStatus), default=SkillLifecycleStatus.ACTIVE, index=True
    )
    # pinned выводит скилл из-под любых автоматических переходов (§18.1).
    pinned: Mapped[bool] = mapped_column(default=False)
    last_used_at: Mapped[datetime | None]
    archived_at: Mapped[datetime | None]
    note: Mapped[str | None] = mapped_column(Text)


class Artifact(TimestampedBase):
    """Ссылка на артефакт run'а; содержимое — в Git/файловой системе."""

    __tablename__ = "artifacts"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(1024))
    kind: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ErrorEvent(TimestampedBase):
    """Ошибка компонента платформы или инструмента (§15)."""

    __tablename__ = "error_events"

    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    source: Mapped[str] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
