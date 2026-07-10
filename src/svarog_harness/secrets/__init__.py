"""Секреты: secret scan, denylist путей, SecretStore, redaction (ADR-0006)."""

from pathlib import Path

from svarog_harness.secrets.denylist import (
    SECRET_PATH_PATTERNS,
    gitignore_block,
    is_secret_path,
)
from svarog_harness.secrets.redaction import redact
from svarog_harness.secrets.scanner import (
    SecretFinding,
    scan_files,
    scan_text,
    shannon_entropy,
)
from svarog_harness.secrets.store import (
    EnvSecretStore,
    FileSecretStore,
    LayeredSecretStore,
    SecretStore,
    injected_env,
    selected_values,
)

__all__ = [
    "SECRET_PATH_PATTERNS",
    "EnvSecretStore",
    "FileSecretStore",
    "LayeredSecretStore",
    "SecretFinding",
    "SecretStore",
    "gitignore_block",
    "injected_env",
    "is_secret_path",
    "redact",
    "scan_files",
    "scan_text",
    "selected_values",
    "shannon_entropy",
]


def default_secret_store(path: "Path | None", *, env_fallback: bool = True) -> SecretStore:
    """Store из файла (если путь задан) + опциональный env-fallback (ADR-0006).

    `env_fallback=False` (кламп роли `standard`, ADR-0014) убирает
    `EnvSecretStore`, чтобы `get(ref)` не проваливался в хостовый `os.environ` —
    иначе tenant-ref, совпавший с именем хостовой переменной, утёк бы в sandbox.
    """
    from svarog_harness.secrets.store import EnvSecretStore, FileSecretStore, LayeredSecretStore

    stores: list[SecretStore] = []
    if path is not None:
        stores.append(FileSecretStore(path.expanduser()))
    if env_fallback:
        stores.append(EnvSecretStore())
    return LayeredSecretStore(stores)
