"""Паттерн-эвристики опасных bash-команд — UX-слой поверх sandbox (ADR-0002).

Адаптировано из hermes-agent `tools/approval.py` DANGEROUS_PATTERNS (MIT,
NousResearch; см. docs/reference-analysis.md). Роль эвристик строго
ограничена (§3.6, ADR-0010): совпадение эскалирует риск bash-команды до
high (→ notify в yolo/auto, require_approval в supervised) и никогда не
участвует в critical-наборе — статическая классификация shell-команд
принципиально ненадежна, гарантии дает только слой 1 sandbox. Deobfuscation
hermes (варианты нормализации команды) намеренно опущена: best-effort слой
не притворяется надежным.
"""

import re

# (паттерн, описание для trace/notify). Регистронезависимо.
_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+(-[^\s]*\s+)*/", "удаление по абсолютному пути"),
    (r"\brm\s+-[^\s]*r", "рекурсивное удаление"),
    (r"\brm\s+--recursive\b", "рекурсивное удаление"),
    (r"\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b", "права на запись для всех"),
    (r"\bchown\s+(-[^\s]*)?R\b", "рекурсивная смена владельца"),
    (r"\bmkfs(\.[a-z0-9]+)?\b", "форматирование файловой системы"),
    (r"\bdd\s+[^\n]*\bof=/dev/", "запись на блочное устройство (dd)"),
    (r">\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)", "запись на блочное устройство"),
    (r"\bDROP\s+(TABLE|DATABASE)\b", "SQL DROP"),
    (r"\bDELETE\s+FROM\b(?![^\n]*\bWHERE\b)", "SQL DELETE без WHERE"),
    (r"\bTRUNCATE\s+(TABLE)?\s*\w", "SQL TRUNCATE"),
    (r"\bkill\s+(-[^\s]+\s+)*-1\b", "убийство всех процессов"),
    (r"\bpkill\s+-9\b", "принудительное убийство процессов"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\b(curl|wget)\b[^\n|]*\|\s*(?:[/\w]*/)?(?:ba|z|k)?sh\b", "исполнение удаленного скрипта"),
    (
        r"(?:\beval\b|\bsource\b)\s*(?:\$\(\s*|`\s*)(?:curl|wget)\b",
        "исполнение удаленного скрипта через подстановку",
    ),
    (
        r"\b(base64|base32)\s+(?:-[dD]|--decode)\b[^\n|]*\|\s*(bash|sh|zsh|ksh|dash)\b",
        "декодирование в shell (возможная обфускация)",
    ),
    (r">>?\s*['\"]?/etc/", "запись в системную конфигурацию"),
    (r"\btee\b[^\n]*\s['\"]?/etc/", "запись в системную конфигурацию (tee)"),
    (r"\bsed\s+-[^\s]*i[^\n]*\s/etc/", "правка системной конфигурации (sed -i)"),
    (
        r">>?\s*['\"]?(?:~|\$\{?HOME\}?)/\.(ssh|bashrc|zshrc|profile|netrc)",
        "запись в ~/.ssh или shell rc",
    ),
    (r"\bsystemctl\s+(-[^\s]+\s+)*(stop|restart|disable|mask)\b", "остановка/перезапуск сервиса"),
    (r"\bgit\s+push\b[^\n]*(\s--force\b|\s-f\b)", "git push --force"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard"),
    (r"\bfind\b[^\n]*-(delete|exec(?:dir)?\s+(/\S*/)?rm)\b", "массовое удаление через find"),
    (r"\bxargs\b[^\n]*\brm\b", "массовое удаление через xargs"),
    (r"^\s*(sudo\s+)?(shutdown|reboot|halt|poweroff)\b", "выключение/перезагрузка системы"),
]

_COMPILED = [
    (re.compile(pattern, re.IGNORECASE), description)
    for pattern, description in _DANGEROUS_PATTERNS
]


def detect_dangerous_command(command: str) -> str | None:
    """Описание первого совпавшего паттерна или None. Best-effort (ADR-0002)."""
    for pattern, description in _COMPILED:
        if pattern.search(command):
            return description
    return None


_GIT_PUSH = re.compile(r"\bgit\s+push\b", re.IGNORECASE)


def detect_protected_push(command: str, protected_branches: frozenset[str]) -> str | None:
    """`git push`, целящий в защищённую ветку → описание, иначе None.

    Дополняет типизированный git.push_protected (Flow C): внутри run агент
    пушит через bash, который сам по себе не знает про protected-набор. Слой
    строго best-effort (ADR-0002): статическое сопоставление имени ветки в
    команде эскалирует риск до high (→ approval в supervised), но не даёт
    гарантии — недоверенное должно идти в docker-sandbox.
    """
    if not _GIT_PUSH.search(command):
        return None
    for branch in protected_branches:
        if re.search(rf"\bgit\s+push\b[^\n]*\b{re.escape(branch)}\b", command, re.IGNORECASE):
            return f"push в защищённую ветку '{branch}'"
    return None
