"""Типы control-plane тенантов (ADR-0012/0014)."""

from dataclasses import dataclass, field

from svarog_harness.config.schema import TenantRole


@dataclass(frozen=True)
class TenantContext:
    """Результат резолвинга principal'а: кому принадлежит запрос и с какой ролью.

    Интерфейсы (gateway, Telegram, CLI) получают его из реестра и передают в
    резолвинг cfg. Роль отсюда — источник истины для клампа (ADR-0013).
    """

    tenant_id: str
    role: TenantRole


@dataclass
class TenantRecord:
    """Запись реестра о тенанте.

    `principals` — список идентификаторов вида `telegram:<id>` / `gateway:<tok>`
    / `cli:<name>`, привязанных к тенанту. `quotas` — задел под Фазу 3.
    """

    tenant_id: str
    role: TenantRole
    created_at: str  # ISO-8601 UTC
    principals: list[str] = field(default_factory=list)
    quotas: dict[str, object] = field(default_factory=dict)
