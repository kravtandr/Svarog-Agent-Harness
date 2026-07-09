"""Обязательный secret scan перед commit во всех трёх git flow (ADR-0006, §12).

Секрет в staged-изменениях блокирует commit, а не предупреждает. Та же
проверка повторяется перед push (вторая линия во Flow C).
"""

from svarog_harness.gitflow.repo import GitRepo
from svarog_harness.secrets import SecretFinding, scan_files


class SecretScanBlockedError(Exception):
    """Secret scan нашёл секреты в staged-изменениях; commit заблокирован."""

    def __init__(self, findings: list[SecretFinding]) -> None:
        self.findings = findings
        lines = [f"  {f.path}:{f.line} [{f.rule}] {f.excerpt}" for f in findings]
        super().__init__("secret scan заблокировал commit:\n" + "\n".join(lines))


async def scan_staged(
    repo: GitRepo, *, known_values: frozenset[str] = frozenset()
) -> list[SecretFinding]:
    """Просканировать staged-содержимое (denylist путей + контент)."""
    files = {path: await repo.read_staged(path) for path in await repo.staged_files()}
    return scan_files(files, known_values=known_values)


async def scan_ref(
    repo: GitRepo, ref: str, *, known_values: frozenset[str] = frozenset()
) -> list[SecretFinding]:
    """Просканировать содержимое git ref перед push."""
    _, out, _ = await repo._git("ls-tree", "-r", "--name-only", ref)
    files: dict[str, str] = {}
    for path in out.splitlines():
        if not path:
            continue
        _, content, _ = await repo._git("show", f"{ref}:{path}", check=False)
        files[path] = content
    return scan_files(files, known_values=known_values)


async def commit_guarded(
    repo: GitRepo,
    message: str,
    *,
    known_values: frozenset[str] = frozenset(),
    trailers: dict[str, str] | None = None,
) -> str:
    """Закоммитить staged-изменения только если secret scan чист."""
    findings = await scan_staged(repo, known_values=known_values)
    if findings:
        raise SecretScanBlockedError(findings)
    return await repo.commit(message, trailers=trailers)
