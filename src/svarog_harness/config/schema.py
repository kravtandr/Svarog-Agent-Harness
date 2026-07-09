"""Pydantic-схема `svarog.yaml` (§13 TASK.md).

Все модели запрещают неизвестные ключи (`extra="forbid"`) — опечатка в
конфигурации должна падать при загрузке, а не молча игнорироваться.
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class AutonomyMode(StrEnum):
    """Режимы автономии (ADR-0010); фиксируются при старте run."""

    SUPERVISED = "supervised"
    AUTO = "auto"
    YOLO = "yolo"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderConfig(StrictModel):
    type: Literal["openai-compatible"] = "openai-compatible"
    base_url: str
    model: str
    # Именованная ссылка на секрет в SecretStore, не сам ключ (ADR-0006):
    # значение берётся из secrets-файла или env на execution-слое.
    api_key_ref: str | None = None
    # Цены за миллион токенов — для учета стоимости run; 0 = локальная модель.
    input_usd_per_mtok: float = Field(default=0.0, ge=0)
    output_usd_per_mtok: float = Field(default=0.0, ge=0)
    timeout_sec: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=2, ge=0)


class ModelsConfig(StrictModel):
    default: str
    # Дешевая модель для служебных задач: curator, компакция, verifier-judge.
    auxiliary: str | None = None
    providers: dict[str, ProviderConfig]

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        known = set(self.providers)
        if self.default not in known:
            raise ValueError(
                f"models.default '{self.default}' отсутствует в models.providers "
                f"(определены: {sorted(known) or 'нет'})"
            )
        if self.auxiliary is not None and self.auxiliary not in known:
            raise ValueError(f"models.auxiliary '{self.auxiliary}' отсутствует в models.providers")
        return self

    @property
    def auxiliary_or_default(self) -> str:
        return self.auxiliary if self.auxiliary is not None else self.default


class RuntimeConfig(StrictModel):
    autonomy: AutonomyMode = AutonomyMode.YOLO
    max_iterations: int = Field(default=50, gt=0)
    max_context_tokens: int = Field(default=120_000, gt=0)
    refuel_after_iterations: int = Field(default=35, gt=0)
    max_tokens_per_run: int = Field(default=2_000_000, gt=0)
    max_cost_usd_per_run: float = Field(default=5.0, gt=0)

    @model_validator(mode="after")
    def _check_refuel_threshold(self) -> Self:
        if self.refuel_after_iterations >= self.max_iterations:
            raise ValueError(
                f"runtime.refuel_after_iterations ({self.refuel_after_iterations}) должен быть "
                f"меньше runtime.max_iterations ({self.max_iterations}), иначе refuel не сработает"
            )
        return self


class SandboxConfig(StrictModel):
    type: Literal["docker", "local-trusted"] = "docker"
    network: Literal["disabled"] = "disabled"  # allowlist-режим — пост-MVP (ADR-0002)
    image: str = "python:3.12-slim"  # нужен bash и coreutils (timeout)
    memory_limit: str = "8g"
    cpu_limit: float = Field(default=4, gt=0)
    timeout_sec: int = Field(default=120, gt=0)


class GitConfig(StrictModel):
    auto_pull: bool = True
    auto_commit: bool = True
    require_approval_for_push: bool = True
    # Для репозиториев с публичным remote отключение игнорируется gitflow-слоем (ADR-0006).
    secret_scan_before_commit: bool = True


class SkillsConfig(StrictModel):
    paths: list[Path] = Field(default_factory=lambda: [Path("./skills"), Path("~/.svarog/skills")])
    auto_load_full_content: bool = False


class StorageConfig(StrictModel):
    # SQLite по умолчанию (ADR-0007); Postgres — в server-режимах пост-MVP.
    db_path: Path = Path("~/.svarog/svarog.db")


class MemoryConfig(StrictModel):
    # Каталог memory-репозитория (Flow A, ADR-0003/0004); None — память выключена.
    path: Path | None = None
    # Лимит memory-entrypoint в контексте (§6.7), чтобы не раздувать промпт.
    context_limit_bytes: int = Field(default=16_000, gt=0)


class SecretsConfig(StrictModel):
    # Файл секретов {имя: значение}, права 0600, вне репозитория (ADR-0006).
    # None — только env-fallback.
    path: Path | None = Path("~/.svarog/secrets.json")
    # Имена секретов, явно выдаваемых в окружение sandbox (§12, «только явно выданные»).
    inject: list[str] = Field(default_factory=list)


class CheckSpec(StrictModel):
    name: str
    # Shell-команда проверки; исполняется в sandbox после run (§6.11).
    command: str


class VerifierConfig(StrictModel):
    # Детерминированные проверки после run'а; приоритет над самооценкой агента (§6.11).
    checks: list[CheckSpec] = Field(default_factory=list)
    # Secret scan рабочего дерева всегда выполняется (ADR-0006); можно не отключать.
    secret_scan: bool = True


class MCPServerConfig(StrictModel):
    # stdio-транспорт: команда и аргументы запуска MCP-сервера.
    command: str
    args: list[str] = Field(default_factory=list)
    # Имена секретов из SecretStore → env сервера (ADR-0006), не значения.
    env_refs: list[str] = Field(default_factory=list)
    # Риск по умолчанию для инструментов сервера: high + approval (§9), пока
    # администратор не ослабит профилем notify.
    risk: Literal["low", "medium", "high", "critical"] = "high"


class MCPConfig(StrictModel):
    servers: dict[str, MCPServerConfig] = Field(default_factory=dict)


class CuratorConfig(StrictModel):
    # Слой 1 (§18.1, ADR-0009): скилл без использования N дней → stale, больше → archived.
    stale_after_days: int = Field(default=30, gt=0)
    archive_after_days: int = Field(default=90, gt=0)
    # Слой 2 (LLM-консолидация) выключен по умолчанию — opt-in (ADR-0009).
    semantic: bool = False

    @model_validator(mode="after")
    def _check_thresholds(self) -> Self:
        if self.archive_after_days <= self.stale_after_days:
            raise ValueError(
                f"curator.archive_after_days ({self.archive_after_days}) должен быть больше "
                f"stale_after_days ({self.stale_after_days})"
            )
        return self


class TelegramConfig(StrictModel):
    # Имя секрета с bot-токеном в SecretStore (ADR-0006), не сам токен: проект
    # публичный, токен в конфиге/истории = скомпрометирован. None — бот выключен.
    token_ref: str | None = None
    # Allowlist Telegram user-id, которым разрешён доступ (§16 auth). Пустой —
    # бот отвечает всем отказом: интернет-facing интерфейс без allowlist опасен.
    allowed_users: list[int] = Field(default_factory=list)
    # Таймаут long-polling getUpdates (сек).
    poll_timeout_sec: int = Field(default=30, ge=0)


class GatewayConfig(StrictModel):
    # Bearer-token для REST/WebSocket gateway. Значение хранится в SecretStore;
    # без токена CLI разрешает serve только на loopback-адресах.
    token_ref: str | None = None


class PolicyProfile(StrictModel):
    require_approval: list[str] = Field(default_factory=list)
    notify: list[str] = Field(default_factory=list)


class PoliciesConfig(StrictModel):
    # Неотключаемый critical-набор (§3.6) в конфигурации не перечисляется.
    protected_branches: list[str] = Field(default_factory=lambda: ["main", "production"])
    profiles: dict[str, PolicyProfile] = Field(default_factory=dict)


class SvarogConfig(BaseSettings):
    """Корень конфигурации: merge user- и project-уровней + env-переменные.

    Переменные окружения `SVAROG_*` (вложенность через `__`, например
    `SVAROG_RUNTIME__AUTONOMY=supervised`) имеют приоритет над файлами.
    """

    model_config = SettingsConfigDict(
        extra="forbid",
        env_prefix="SVAROG_",
        env_nested_delimiter="__",
    )

    models: ModelsConfig
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    policies: PoliciesConfig = Field(default_factory=PoliciesConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    curator: CuratorConfig = Field(default_factory=CuratorConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # env поверх значений из yaml-файлов (init); .env-файлы не читаем — ADR-0006.
        return (env_settings, init_settings)
