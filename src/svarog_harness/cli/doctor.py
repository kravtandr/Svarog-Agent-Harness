"""`svarog doctor` — read-only диагностика окружения (ADR-0015 фаза 5).

Ничего не создаёт и не чинит: каждый пункт — ok/warn/fail с подсказкой,
как исправить руками. fail — работать нельзя (exit 1); warn — работать
можно, но с деградацией (Python-fallback поиска, отложенные миграции).
"""

import os
import shutil
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from alembic.script import ScriptDirectory

from svarog_harness.config.loader import ConfigError, load_config
from svarog_harness.config.paths import memory_dir, workspace_layout_violations
from svarog_harness.config.schema import SvarogConfig
from svarog_harness.llm.openai_compatible import ApiKeyError, resolve_api_key
from svarog_harness.secrets import default_secret_store
from svarog_harness.storage.db import alembic_config

Status = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: Status
    detail: str
    hint: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def collect_checks(workspace: Path) -> list[DoctorCheck]:
    """Все проверки; порядок стабилен для людей и скриптов."""
    checks: list[DoctorCheck] = []
    cfg = _check_config(workspace, checks)
    checks.append(_check_git())
    checks.append(_check_workspace_repo(workspace))
    if cfg is not None:
        checks.append(_check_layout(cfg, workspace))
        checks.append(_check_db(cfg))
        checks.append(_check_sandbox(cfg))
        checks.append(_check_model(cfg))
        checks.append(_check_memory(cfg))
    checks.append(_check_ripgrep())
    checks.append(_check_agent_orphans())
    return checks


def _pid_alive(pid: str) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (ValueError, ProcessLookupError, PermissionError):
        # PermissionError: чужой живой процесс — но owner-pid svarog всегда
        # процесс этого же пользователя, значит это не наш владелец.
        return False
    return True


def find_agent_orphans(run=subprocess.run) -> tuple[list[str], list[str]]:
    """Ресурсы svarog-agent=1 без живого владельца (svarog-owner-pid).

    Ресурсы, созданные до появления reaper'а, метки owner не имеют и не
    подметаются никогда (кампания 21.07.2026: 4 legacy-сироты роняли
    test_external_docker) — их находит этот шаг.
    """
    fmt = '{{.Names}}\t{{.Label "svarog-owner-pid"}}'
    out = run(
        ["docker", "ps", "-a", "--filter", "label=svarog-agent=1", "--format", fmt],
        capture_output=True,
        text=True,
    ).stdout
    containers = [
        name
        for line in out.splitlines()
        if (name := line.split("\t")[0]) and not _pid_alive(line.split("\t")[1] if "\t" in line else "")
    ]
    nfmt = '{{.Name}}\t{{index .Labels "svarog-owner-pid"}}'
    nout = run(
        ["docker", "network", "ls", "--filter", "label=svarog-agent=1", "--format", nfmt],
        capture_output=True,
        text=True,
    ).stdout
    networks = [
        name
        for line in nout.splitlines()
        if (name := line.split("\t")[0]) and not _pid_alive(line.split("\t")[1] if "\t" in line else "")
    ]
    return containers, networks


def remove_agent_orphans(
    containers: list[str], networks: list[str], run=subprocess.run
) -> None:
    """Удалить найденных сирот (вызывается ТОЛЬКО по явному --clean-orphans)."""
    if containers:
        run(["docker", "rm", "-f", *containers], capture_output=True, text=True)
    if networks:
        run(["docker", "network", "rm", *networks], capture_output=True, text=True)


def _check_agent_orphans() -> DoctorCheck:
    if shutil.which("docker") is None:
        return DoctorCheck("agent-orphans", "ok", "docker отсутствует — проверка не нужна")
    try:
        containers, networks = find_agent_orphans()
    except OSError as exc:
        return DoctorCheck("agent-orphans", "warn", f"docker недоступен: {exc}")
    if not containers and not networks:
        return DoctorCheck("agent-orphans", "ok", "осиротевших ресурсов агентов нет")
    listing = ", ".join(containers + networks)
    return DoctorCheck(
        "agent-orphans",
        "warn",
        f"осиротевшие ресурсы svarog-agent: {listing}",
        hint="удалить: svarog doctor --clean-orphans",
    )


def _check_config(workspace: Path, checks: list[DoctorCheck]) -> SvarogConfig | None:
    try:
        cfg = load_config(project_dir=workspace)
    except ConfigError as exc:
        checks.append(
            DoctorCheck(
                "config",
                "fail",
                f"конфиг не читается: {exc}",
                "поправьте svarog.yaml или пересоздайте его через `svarog init`",
            )
        )
        return None
    checks.append(DoctorCheck("config", "ok", "svarog.yaml прочитан и валиден"))
    return cfg


def _check_git() -> DoctorCheck:
    git = shutil.which("git")
    if git is None:
        return DoctorCheck(
            "git",
            "fail",
            "git не найден в PATH",
            "установите git — без него Flow A/B/C не работают",
        )
    try:
        out = subprocess.run(
            [git, "--version"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        return DoctorCheck("git", "fail", f"git найден, но не отвечает: {exc}")
    return DoctorCheck("git", "ok", out)


def _check_workspace_repo(workspace: Path) -> DoctorCheck:
    if (workspace / ".git").exists():
        return DoctorCheck("workspace", "ok", "workspace — git-репозиторий (Flow C активен)")
    return DoctorCheck(
        "workspace",
        "warn",
        "workspace не является git-репозиторием",
        "step-коммиты, rewind и child runs требуют git: выполните `git init`",
    )


def _check_layout(cfg: SvarogConfig, workspace: Path) -> DoctorCheck:
    violations = workspace_layout_violations(cfg, workspace)
    if not violations:
        return DoctorCheck("layout", "ok", "control-plane вне agent-writable дерева")
    detail = "; ".join(violations)
    if cfg.sandbox.type == "docker":
        return DoctorCheck(
            "layout", "fail", detail, "в docker-режиме такой run будет отклонён (ADR-0015 §0.3)"
        )
    return DoctorCheck(
        "layout", "warn", detail, "перенесите state за пределы workspace (ADR-0015 §0.3)"
    )


def _check_db(cfg: SvarogConfig) -> DoctorCheck:
    db_path = cfg.storage.db_path.expanduser()
    if not db_path.exists():
        return DoctorCheck(
            "db",
            "warn",
            f"БД ещё нет ({db_path})",
            "будет создана с миграциями при первом `svarog run`/`init`",
        )
    head = ScriptDirectory.from_config(alembic_config(db_path)).get_current_head()
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.DatabaseError as exc:
        return DoctorCheck("db", "fail", f"БД не читается: {exc}", "проверьте файл или удалите его")
    current = row[0] if row else None
    if current != head:
        return DoctorCheck(
            "db",
            "warn",
            f"миграции отстают (current={current}, head={head})",
            "применятся автоматически при старте runtime",
        )
    return DoctorCheck("db", "ok", f"схема актуальна ({head})")


def _check_sandbox(cfg: SvarogConfig) -> DoctorCheck:
    if cfg.sandbox.type == "local-trusted":
        return DoctorCheck(
            "sandbox", "ok", "local-trusted: исполнение на хосте без изоляции (доверенная машина)"
        )
    docker = shutil.which("docker")
    if docker is None:
        return DoctorCheck(
            "sandbox",
            "fail",
            "sandbox.type=docker, но docker не найден в PATH",
            "установите docker или переключитесь на local-trusted",
        )
    probe = subprocess.run(
        [docker, "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if probe.returncode != 0:
        return DoctorCheck(
            "sandbox",
            "fail",
            f"docker есть, но daemon не отвечает: {probe.stderr.strip()}",
            "запустите docker daemon",
        )
    return DoctorCheck("sandbox", "ok", f"docker server {probe.stdout.strip()}")


def _check_model(cfg: SvarogConfig) -> DoctorCheck:
    provider_cfg = cfg.models.providers[cfg.models.default]
    if provider_cfg.api_key_ref is None:
        return DoctorCheck(
            "model", "ok", f"{cfg.models.default}: локальная модель, ключ не требуется"
        )
    store = default_secret_store(cfg.secrets.path, env_fallback=True)
    try:
        resolve_api_key(provider_cfg, store)
    except ApiKeyError as exc:
        return DoctorCheck(
            "model", "fail", str(exc), f"`svarog secrets set {provider_cfg.api_key_ref}`"
        )
    return DoctorCheck(
        "model", "ok", f"{cfg.models.default}: ключ '{provider_cfg.api_key_ref}' найден"
    )


def _check_memory(cfg: SvarogConfig) -> DoctorCheck:
    mem = memory_dir(cfg)
    if mem is None:
        return DoctorCheck("memory", "ok", "память выключена конфигом")
    if mem.expanduser().is_dir():
        return DoctorCheck("memory", "ok", f"каталог памяти на месте ({mem})")
    return DoctorCheck(
        "memory", "warn", f"каталога памяти ещё нет ({mem})", "создаётся при `svarog init`"
    )


def _check_ripgrep() -> DoctorCheck:
    if shutil.which("rg") is not None:
        return DoctorCheck("ripgrep", "ok", "rg найден — быстрый search_files")
    return DoctorCheck(
        "ripgrep",
        "warn",
        "rg не найден — search_files работает через медленный Python-fallback",
        "установите ripgrep (apt install ripgrep / brew install ripgrep)",
    )
