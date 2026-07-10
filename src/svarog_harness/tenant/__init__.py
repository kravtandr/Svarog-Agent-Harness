"""Мультитенантность control-plane (ADR-0012/0014).

Реестр тенантов, резолвинг principal'ов и индекс run→tenant. Резолвинг путей и
кламп роли живут в `config/paths.py` (чистые функции над cfg).
"""

from svarog_harness.tenant.models import TenantContext, TenantRecord
from svarog_harness.tenant.registry import (
    PrincipalConflictError,
    TenantExistsError,
    TenantRegistry,
    TenantRegistryError,
)

__all__ = [
    "PrincipalConflictError",
    "TenantContext",
    "TenantExistsError",
    "TenantRecord",
    "TenantRegistry",
    "TenantRegistryError",
]
