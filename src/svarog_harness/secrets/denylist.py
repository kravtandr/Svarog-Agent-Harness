"""Denylist путей секретов и генерация .gitignore (ADR-0006, §12).

Файлы секретов не должны попадать ни в write_file, ни в commit. `svarog init`
(#19) пишет эти паттерны в .gitignore; git-flow отвергает staged-файлы,
совпавшие с denylist, ещё до сканирования содержимого.
"""

from fnmatch import fnmatch

# glob-паттерны по basename или относительному пути (fnmatch).
SECRET_PATH_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "*.keystore",
    ".netrc",
    ".pgpass",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "service-account*.json",
    ".svarog/secrets*",
)

# .env.example и подобные шаблоны — не секреты, исключаем из denylist.
_ALLOW_SUFFIXES = (".example", ".sample", ".template", ".dist")


def is_secret_path(relative_path: str) -> bool:
    """Совпадает ли путь с denylist секретов (по basename и полному пути)."""
    normalized = relative_path.replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1]
    if any(basename.endswith(suffix) for suffix in _ALLOW_SUFFIXES):
        return False
    return any(
        fnmatch(basename, pattern) or fnmatch(normalized, pattern)
        for pattern in SECRET_PATH_PATTERNS
    )


def gitignore_block() -> str:
    """Блок .gitignore, покрывающий файлы секретов (для svarog init)."""
    lines = ["# секреты — не коммитить (ADR-0006)", *SECRET_PATH_PATTERNS]
    return "\n".join(lines) + "\n"
