"""Secret scanner (ADR-0006, §12): блокирующая проверка перед commit и push.

Три сигнала: (1) паттерны известных форматов ключей, (2) entropy-эвристика
для присвоенных/закавыченных высокоэнтропийных токенов, (3) точные значения
из SecretStore (интерфейс — параметр known_values; сам store — M4). Репозиторий
публичный: обнаружение секрета блокирует commit, а не предупреждает.

Redaction — best-effort вторая линия (ADR-0006); основная гарантия —
невыдача секрета без approval на policy-слое.
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from svarog_harness.secrets.denylist import is_secret_path

# (имя правила, компилированный паттерн). Значения в тестах — заведомо
# ненастоящие (публичные example-токены и повторяющиеся символы).
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{36,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[0-9A-Za-z_]{60,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "private-key-block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    ),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+")),
]

# Присвоение секрета: ключ с секретным именем = высокоэнтропийное значение.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|
        private[_-]?key|client[_-]?secret|auth|credential)s?
    \s*[:=]\s*
    ['"]?(?P<value>[A-Za-z0-9+/_\-]{16,})['"]?
    """
)

# Минимальная энтропия (бит/символ) значения в присвоении, чтобы счесть секретом.
_MIN_ENTROPY = 3.5
# Плейсхолдеры, которые не считаем секретами даже в присвоении.
_PLACEHOLDER = re.compile(
    r"^(?:x{3,}|\*{3,}|\.{3,}|changeme|example|placeholder|your[_-].*|"
    r"none|null|true|false|env\(.*\)|\$\{.*\})$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SecretFinding:
    path: str
    line: int
    rule: str
    # Значение уже вырезано (redaction): показываем только маркер (§12).
    excerpt: str


def shannon_entropy(text: str) -> float:
    """Энтропия Шеннона (бит/символ); 0 для пустой строки."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    length = len(text)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _redact(value: str) -> str:
    """Показать длину и первые пару символов, скрыв значение."""
    head = value[:2]
    return f"{head}… [вырезано {len(value)} символов]"


def scan_text(
    text: str, *, path: str = "<text>", known_values: frozenset[str] = frozenset()
) -> list[SecretFinding]:
    """Найти секреты в тексте. known_values — точные значения из SecretStore."""
    findings: list[SecretFinding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for value in known_values:
            if value and value in line:
                findings.append(SecretFinding(path, lineno, "secretstore-value", _redact(value)))
        for rule, pattern in _PATTERNS:
            match = pattern.search(line)
            if match is not None:
                findings.append(SecretFinding(path, lineno, rule, _redact(match.group(0))))
        assignment = _ASSIGNMENT.search(line)
        if assignment is not None:
            value = assignment.group("value")
            if (
                not _PLACEHOLDER.match(value)
                and shannon_entropy(value) >= _MIN_ENTROPY
                # Пропускаем моно-регистровый hex (git SHA, md5/sha-хэши).
                and not re.fullmatch(r"[0-9a-f]+", value)
                and not re.fullmatch(r"[0-9A-F]+", value)
            ):
                findings.append(
                    SecretFinding(path, lineno, "high-entropy-assignment", _redact(value))
                )
    return findings


def scan_files(
    files: Mapping[str, str], *, known_values: frozenset[str] = frozenset()
) -> list[SecretFinding]:
    """Гейт перед commit/push: denylist путей + сканирование содержимого.

    Файл, совпавший с denylist секретов (ADR-0006), — отдельная находка
    независимо от содержимого: такие файлы вообще не должны коммититься.
    """
    findings: list[SecretFinding] = []
    for path in sorted(files):
        if is_secret_path(path):
            findings.append(SecretFinding(path, 0, "secret-file-path", "файл в denylist секретов"))
        findings.extend(scan_text(files[path], path=path, known_values=known_values))
    return findings
