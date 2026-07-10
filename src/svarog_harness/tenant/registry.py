"""TenantRegistry — реестр тенантов control-plane (ADR-0012/0014).

MVP-бэкенд: JSON-файл `agent-home/tenants.json` под межпроцессным
advisory-lock'ом (`fcntl.flock`), запись атомарна через временный файл +
`os.replace`. Паттерн «файл сейчас, pluggable потом» — как locks/secrets/
storage (ADR-0007). Реестр — единственная разделяемая на всех тенантов точка
записи; операции редки (регистрация, привязка principal'а, отметка run'а),
поэтому грубого файлового лока достаточно.

Хранит:
* `tenants[id]` — запись тенанта (роль, дата, principals, quotas);
* `index[principal] -> id` — обратный индекс для auth-резолвинга;
* `run_index[run_id] -> id` — маршрутизация resume/refuel-супервизора (ADR-0005).

Чтение идёт без лока: `os.replace` атомарен, читатель видит либо старый, либо
новый файл целиком.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from svarog_harness.config.schema import TenantRole
from svarog_harness.tenant.models import TenantContext, TenantRecord

try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover — не-POSIX платформа
    _HAS_FCNTL = False

_VERSION = 1


class TenantRegistryError(Exception):
    """Базовая ошибка реестра тенантов."""


class TenantExistsError(TenantRegistryError):
    """Тенант с таким id уже зарегистрирован."""


class PrincipalConflictError(TenantRegistryError):
    """Principal уже привязан к другому тенанту."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _record_to_json(record: TenantRecord) -> dict[str, Any]:
    return {
        "role": record.role.value,
        "created_at": record.created_at,
        "principals": list(record.principals),
        "quotas": dict(record.quotas),
    }


def _record_from_json(tenant_id: str, raw: dict[str, Any]) -> TenantRecord:
    return TenantRecord(
        tenant_id=tenant_id,
        role=TenantRole(raw["role"]),
        created_at=raw.get("created_at", ""),
        principals=list(raw.get("principals", [])),
        quotas=dict(raw.get("quotas", {})),
    )


class TenantRegistry:
    """Реестр тенантов поверх JSON-файла с межпроцессной сериализацией записи."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser()
        self._lock_path = self._path.with_name(self._path.name + ".lock")

    # --- чтение (без лока) -------------------------------------------------

    def get(self, tenant_id: str) -> TenantRecord | None:
        raw = self._load()["tenants"].get(tenant_id)
        return _record_from_json(tenant_id, raw) if raw is not None else None

    def resolve_principal(self, principal: str) -> TenantContext | None:
        """principal (`telegram:123` / `gateway:tok` / `cli:local`) → контекст."""
        data = self._load()
        tenant_id = data["index"].get(principal)
        if tenant_id is None:
            return None
        rec = data["tenants"].get(tenant_id)
        if rec is None:  # осиротевший индекс — трактуем как отсутствие доступа
            return None
        return TenantContext(tenant_id=tenant_id, role=TenantRole(rec["role"]))

    def tenant_of_run(self, run_id: str) -> str | None:
        value = self._load()["run_index"].get(run_id)
        return value if isinstance(value, str) else None

    def active_tenant_ids(self) -> list[str]:
        """Тенанты, у которых есть зарегистрированные run'ы (для супервизора)."""
        seen: dict[str, None] = {}  # dict сохраняет порядок появления
        for tenant_id in self._load()["run_index"].values():
            if isinstance(tenant_id, str):
                seen.setdefault(tenant_id, None)
        return list(seen)

    def list_tenants(self) -> list[TenantRecord]:
        data = self._load()
        return [_record_from_json(tid, raw) for tid, raw in data["tenants"].items()]

    # --- запись (под локом) ------------------------------------------------

    def create(self, tenant_id: str, role: TenantRole) -> TenantRecord:
        with self._locked():
            data = self._load()
            if tenant_id in data["tenants"]:
                raise TenantExistsError(f"тенант '{tenant_id}' уже существует")
            record = TenantRecord(tenant_id=tenant_id, role=role, created_at=_now_iso())
            data["tenants"][tenant_id] = _record_to_json(record)
            self._save(data)
            return record

    def add_principal(self, tenant_id: str, principal: str) -> None:
        with self._locked():
            data = self._load()
            if tenant_id not in data["tenants"]:
                raise TenantRegistryError(f"нет тенанта '{tenant_id}'")
            owner = data["index"].get(principal)
            if owner is not None and owner != tenant_id:
                raise PrincipalConflictError(
                    f"principal '{principal}' уже привязан к '{owner}'"
                )
            data["index"][principal] = tenant_id
            principals = data["tenants"][tenant_id]["principals"]
            if principal not in principals:
                principals.append(principal)
            self._save(data)

    def record_run(self, run_id: str, tenant_id: str) -> None:
        """Отметить владельца run'а — для resume-роутинга и супервизора (ADR-0005)."""
        with self._locked():
            data = self._load()
            data["run_index"][run_id] = tenant_id
            self._save(data)

    # --- внутреннее --------------------------------------------------------

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": _VERSION, "tenants": {}, "index": {}, "run_index": {}}

    def _load(self) -> dict[str, Any]:
        if not self._path.is_file():
            return self._empty()
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TenantRegistryError(f"{self._path}: ожидался JSON-объект")
        for key in ("tenants", "index", "run_index"):
            data.setdefault(key, {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".tenants-", suffix=".tmp")
        tmp_path = Path(tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            tmp_path.replace(self._path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()  # если replace прошёл — tmp уже нет (FileNotFoundError)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not _HAS_FCNTL:  # pragma: no cover — не-POSIX
            yield
            return
        # Низкоуровневый fd + flock, как в storage/locks.py (POSIX advisory-lock).
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
