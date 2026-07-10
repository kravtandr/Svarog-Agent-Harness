"""Квоты тенанта (ADR-0014, Фаза 3): лимиты одновременности и кумулятивные бюджеты.

Enforcement — на создании run'а в gateway: считаем активные run'ы и суммы
стоимости/токенов по БД тенанта, сверяем с `QuotaConfig`. Дефолт берётся из
`tenancy.default_quota`, per-tenant переопределение — из `TenantRecord.quotas`.
"""

from __future__ import annotations

from dataclasses import dataclass

from svarog_harness.config.schema import QuotaConfig
from svarog_harness.tenant.models import TenantRecord


class QuotaExceededError(Exception):
    """Тенант исчерпал лимит — новый run отклоняется (маппится в HTTP 429)."""


@dataclass(frozen=True)
class QuotaUsage:
    active_runs: int
    total_cost_usd: float
    total_tokens: int


def effective_quota(default: QuotaConfig, record: TenantRecord | None) -> QuotaConfig:
    """Дефолтная квота + per-tenant переопределение из `record.quotas`."""
    if record is None or not record.quotas:
        return default
    overrides = {k: v for k, v in record.quotas.items() if k in QuotaConfig.model_fields}
    return default.model_copy(update=overrides) if overrides else default


def check_quota(usage: QuotaUsage, quota: QuotaConfig) -> None:
    """Бросить QuotaExceededError, если использование достигло любого из ненулевых лимитов."""
    if quota.max_concurrent_runs and usage.active_runs >= quota.max_concurrent_runs:
        raise QuotaExceededError(
            f"лимит одновременных run'ов исчерпан ({usage.active_runs}/{quota.max_concurrent_runs})"
        )
    if quota.max_total_cost_usd and usage.total_cost_usd >= quota.max_total_cost_usd:
        raise QuotaExceededError(
            f"бюджет стоимости исчерпан (${usage.total_cost_usd:.4f}/${quota.max_total_cost_usd})"
        )
    if quota.max_total_tokens and usage.total_tokens >= quota.max_total_tokens:
        raise QuotaExceededError(
            f"токен-бюджет исчерпан ({usage.total_tokens}/{quota.max_total_tokens})"
        )
