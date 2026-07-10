"""Gateway: внешние интерфейсы поверх runtime (§6.1, §10).

`GatewayService` не зависит от FastAPI и импортируется всегда; конкретный
транспорт (`api.create_app` — REST/WS, `telegram` — бот) тянет опциональные
зависимости и импортируется по месту, чтобы базовая установка (Git+SQLite,
ADR-0007) не требовала веб-стека.
"""

from svarog_harness.gateway.hub import SingleTenantResolver, TenantHub
from svarog_harness.gateway.service import GatewayService

__all__ = ["GatewayService", "SingleTenantResolver", "TenantHub"]
