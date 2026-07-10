"""Мультитенантность control-plane (ADR-0012/0014).

Реестр тенантов, резолвинг principal'ов и индекс run→tenant. Резолвинг путей и
кламп роли живут в `config/paths.py` (чистые функции над cfg).
"""

from svarog_harness.tenant.models import TenantContext, TenantRecord
from svarog_harness.tenant.provision import (
    GATEWAY_TOKEN_REF,
    ProvisionResult,
    current_token,
    provision_tenant,
    rotate_token,
)
from svarog_harness.tenant.quota import (
    QuotaExceededError,
    QuotaUsage,
    check_quota,
    effective_quota,
)
from svarog_harness.tenant.registry import (
    PrincipalConflictError,
    TenantExistsError,
    TenantRegistry,
    TenantRegistryError,
)

__all__ = [
    "GATEWAY_TOKEN_REF",
    "PrincipalConflictError",
    "ProvisionResult",
    "QuotaExceededError",
    "QuotaUsage",
    "TenantContext",
    "TenantExistsError",
    "TenantRecord",
    "TenantRegistry",
    "TenantRegistryError",
    "check_quota",
    "current_token",
    "effective_quota",
    "provision_tenant",
    "rotate_token",
]
